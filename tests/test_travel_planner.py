"""실제 API 호출 없이 필수 흐름과 실패 정책을 검증한다."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import travel_planner as planner


RECOMMENDATION = {
    "recommended_city": "강릉",
    "weather": "가을의 일반적인 날씨 경향입니다.",
    "events": ["가을 문화행사 후보(일정 확인 필요)"],
    "reason": "바다와 산책을 즐기기 좋습니다. 도심도 함께 둘러볼 수 있습니다.",
}
PLACES = {"documents": [{
    "place_name": "테스트 식당", "road_address_name": "강원 강릉시 테스트로 1",
    "address_name": "강원 강릉시 테스트동 1", "category_name": "음식점 > 한식",
    "place_url": "https://place.map.kakao.com/123", "x": "128.9", "y": "37.7",
}]}
REPORT = """# 2026-10-12 국내 여행 추천 리포트
## 추천 지역
강릉
## 추천 이유
바다와 산책을 즐기기 좋습니다.
## 날씨 요약
가을의 일반적인 날씨 경향입니다.
## 행사/축제
- 가을 문화행사 후보(일정 확인 필요)
## 맛집 추천
- 모델이 잘못 쓴 장소 정보
## 1일 일정 제안
- 오전: 해변 산책
- 오후: 도심 구경
- 저녁: 식사와 휴식
"""


def http_response(payload=None, status=200):
    response = Mock(status_code=status)
    response.json.return_value = payload
    return response


def gemini_response(text):
    return http_response({"candidates": [{
        "finishReason": "STOP", "content": {"parts": [{"text": text}]},
    }]})


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project_patch = patch.object(planner, "PROJECT_DIR", self.root)
        project_patch.start()
        self.addCleanup(project_patch.stop)
        env_patch = patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-gemini-secret", "KAKAO_REST_API_KEY": "test-kakao-secret",
        }, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        # 모든 테스트에서 HTTP를 모의하여 실제 API 호출을 차단한다.
        request_patch = patch.object(planner.requests, "request")
        self.request = request_patch.start()
        self.addCleanup(request_patch.stop)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def execute(self, responses):
        self.request.side_effect = responses
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return planner.main(["--date", "2026-10-12"])

    def raw_data(self):
        paths = list((self.root / "results").glob("*_raw.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def report_text(self):
        paths = list((self.root / "results").glob("*_travel_plan.md"))
        self.assertEqual(len(paths), 1)
        return paths[0].read_text(encoding="utf-8")

    def test_success_connects_city_to_kakao_and_saves_both_results(self):
        code = self.execute([gemini_response(json.dumps(RECOMMENDATION)), http_response(PLACES), gemini_response(REPORT)])
        self.assertEqual(code, 0)
        data = self.raw_data()
        self.assertEqual(data["recommendation"], RECOMMENDATION)
        self.assertEqual(data["place_search_response"], PLACES)
        self.assertEqual(data["restaurants"][0]["x"], 128.9)
        self.assertEqual(data["restaurants"][0]["y"], 37.7)
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["status"], "completed")
        report = self.report_text()
        self.assertIn("테스트 식당", report)
        self.assertIn("강원 강릉시 테스트로 1", report)
        self.assertNotIn("모델이 잘못 쓴 장소 정보", report)
        self.assertIn("## 오류 요약(errors)\n- 없음", report)
        calls = self.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["POST", "GET", "POST"])
        self.assertEqual(calls[1].kwargs["params"]["query"], "강릉 맛집")
        self.assertEqual(calls[1].kwargs["params"]["size"], 5)
        final_prompt = calls[2].kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("테스트 식당", final_prompt)

    def test_invalid_dates_and_missing_option_show_usage_before_api_calls(self):
        for args in ([], ["--date", "2026-2-01"], ["--date", "2026-02-30"], ["-date", "2026-10-12"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as raised:
                    planner.main(args)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("usage:", errors.getvalue())
        self.request.assert_not_called()

    def test_leap_day_and_past_date_are_valid(self):
        self.assertEqual(planner.parse_date("2024-02-29"), "2024-02-29")
        with self.assertRaises(argparse.ArgumentTypeError):
            planner.parse_date("2025-02-29")

    def test_missing_either_key_stops_before_api_and_output_directory(self):
        for name in ("GEMINI_API_KEY", "KAKAO_REST_API_KEY"):
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                with redirect_stderr(self.stderr):
                    self.assertEqual(planner.main(["--date", "2026-10-12"]), 1)
        self.assertIn(".env.example", self.stderr.getvalue())
        self.request.assert_not_called()
        self.assertFalse((self.root / "results").exists())

    def test_dotenv_is_loaded_and_existing_environment_has_priority(self):
        (self.root / ".env").write_text(
            "GEMINI_API_KEY=dotenv-gemini\nKAKAO_REST_API_KEY=dotenv-kakao\nGEMINI_MODEL=gemini-example\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"GEMINI_API_KEY": "environment-key"}, clear=True):
            settings = planner.Settings.from_env()
        self.assertEqual(settings.gemini_key, "environment-key")
        self.assertEqual(settings.kakao_key, "dotenv-kakao")
        self.assertEqual(settings.model, "gemini-example")

    def test_bad_recommendation_is_retried_once_then_recovers(self):
        code = self.execute([
            gemini_response("not JSON"), gemini_response(json.dumps(RECOMMENDATION)),
            http_response(PLACES), gemini_response(REPORT),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(self.request.call_count, 4)
        self.assertEqual(self.raw_data()["errors"][0]["step"], "recommendation")
        self.assertIn("PARSE_ERROR", self.report_text())

    def test_invalid_schema_has_only_one_retry_and_no_place_call(self):
        bad = {**RECOMMENDATION, "events": "행사"}
        code = self.execute([gemini_response(json.dumps(bad)), gemini_response(json.dumps(bad))])
        self.assertEqual(code, 1)
        self.assertEqual(self.request.call_count, 2)
        data = self.raw_data()
        self.assertEqual(len(data["errors"]), 2)
        self.assertIsNone(data["recommendation"])
        self.assertEqual(data["status"], "failed")
        self.assertEqual(list((self.root / "results").glob("*.md")), [])

    def test_missing_required_key_and_wrong_types_are_rejected(self):
        for bad in ([RECOMMENDATION], {"recommended_city": "강릉"},
                    {**RECOMMENDATION, "weather": 123}, {**RECOMMENDATION, "reason": " "},
                    {**RECOMMENDATION, "events": []}, {**RECOMMENDATION, "events": ["a"] * 4}):
            with self.subTest(bad=bad), self.assertRaises(planner.PlannerError):
                planner.validate_recommendation(json.dumps(bad))

    def test_empty_places_continue_and_report_no_data(self):
        code = self.execute([gemini_response(json.dumps(RECOMMENDATION)), http_response({"documents": []}), gemini_response(REPORT)])
        self.assertEqual(code, 0)
        self.assertEqual(self.raw_data()["errors"][0]["type"], "EMPTY_RESULT")
        self.assertEqual(self.raw_data()["restaurants"], [])
        self.assertIn("데이터 없음", self.report_text())
        self.assertNotIn("모델이 잘못 쓴 장소 정보", self.report_text())

    def test_place_auth_and_quota_failures_continue(self):
        for status, kind in ((401, "AUTH_ERROR"), (403, "AUTH_ERROR"), (429, "QUOTA_ERROR")):
            with self.subTest(status=status):
                code = self.execute([gemini_response(json.dumps(RECOMMENDATION)), http_response(status=status), gemini_response(REPORT)])
                self.assertEqual(code, 0)
        raw_paths = list((self.root / "results").glob("*.json"))
        self.assertEqual(len(raw_paths), 3)
        errors = [json.loads(path.read_text())["errors"][0]["type"] for path in raw_paths]
        self.assertCountEqual(errors, ["AUTH_ERROR", "AUTH_ERROR", "QUOTA_ERROR"])
        for path in (self.root / "results").glob("*.md"):
            self.assertIn("데이터 없음", path.read_text())

    def test_place_network_failure_continues(self):
        code = self.execute([
            gemini_response(json.dumps(RECOMMENDATION)),
            requests.ConnectionError("private request details test-kakao-secret"), gemini_response(REPORT),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(self.raw_data()["errors"][0]["type"], "NETWORK_ERROR")
        self.assertIn("데이터 없음", self.report_text())
        self.assertNotIn("private request details", self.stdout.getvalue())

    def test_malformed_place_response_continues(self):
        bad_response = http_response()
        bad_response.json.side_effect = ValueError("server details")
        code = self.execute([gemini_response(json.dumps(RECOMMENDATION)), bad_response, gemini_response(REPORT)])
        self.assertEqual(code, 0)
        self.assertEqual(self.raw_data()["errors"][0]["type"], "PARSE_ERROR")
        self.assertIn("데이터 없음", self.report_text())

    def test_bad_coordinates_continue_with_no_places(self):
        bad_places = {"documents": [{**PLACES["documents"][0], "x": "NaN"}]}
        code = self.execute([gemini_response(json.dumps(RECOMMENDATION)), http_response(bad_places), gemini_response(REPORT)])
        self.assertEqual(code, 0)
        self.assertEqual(self.raw_data()["restaurants"], [])
        self.assertEqual(self.raw_data()["errors"][0]["type"], "PARSE_ERROR")

    def test_llm_auth_failure_is_not_retried(self):
        self.assertEqual(self.execute([http_response(status=403)]), 1)
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.raw_data()["errors"][0]["type"], "AUTH_ERROR")

    def test_final_llm_failure_preserves_intermediate_data(self):
        self.assertEqual(self.execute([
            gemini_response(json.dumps(RECOMMENDATION)), http_response(PLACES), requests.Timeout(),
        ]), 1)
        data = self.raw_data()
        self.assertEqual(data["recommendation"], RECOMMENDATION)
        self.assertEqual(len(data["restaurants"]), 1)
        self.assertEqual(data["errors"][0]["step"], "report")
        self.assertEqual(data["errors"][0]["type"], "TIMEOUT")
        self.assertEqual(list((self.root / "results").glob("*.md")), [])

    def test_incomplete_report_is_not_saved_as_success(self):
        self.assertEqual(self.execute([
            gemini_response(json.dumps(RECOMMENDATION)), http_response(PLACES), gemini_response("# incomplete"),
        ]), 1)
        self.assertEqual(self.raw_data()["status"], "failed")
        self.assertEqual(list((self.root / "results").glob("*.md")), [])

    def test_truncated_gemini_answer_is_rejected(self):
        self.request.return_value = http_response({"candidates": [{
            "finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "unfinished"}]},
        }]})
        with self.assertRaises(planner.PlannerError):
            planner.generate_text(planner.Settings.from_env(), "prompt")

    def test_keys_are_redacted_in_saved_files_and_logs(self):
        recommendation = {**RECOMMENDATION, "recommended_city": "강릉 test-gemini-secret"}
        places = {"documents": [{**PLACES["documents"][0], "place_name": "test-kakao-secret"}]}
        self.assertEqual(self.execute([
            gemini_response(json.dumps(recommendation)), http_response(places), gemini_response(REPORT + "test-gemini-secret"),
        ]), 0)
        texts = self.stdout.getvalue() + self.stderr.getvalue()
        texts += "".join(path.read_text() for path in (self.root / "results").iterdir())
        self.assertNotIn("test-gemini-secret", texts)
        self.assertNotIn("test-kakao-secret", texts)
        self.assertIn("[REDACTED]", texts)
        self.assertNotIn("test-gemini-secret", repr(planner.Settings.from_env()))

    def test_output_write_error_is_handled_without_traceback(self):
        (self.root / "results").write_text("directory blocked", encoding="utf-8")
        with redirect_stderr(self.stderr):
            self.assertEqual(planner.main(["--date", "2026-10-12"]), 1)
        self.request.assert_not_called()
        self.assertIn("파일 권한", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
