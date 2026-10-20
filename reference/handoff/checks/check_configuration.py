"""합성 입력으로 설정 결합 사양을 검사한다. 실제 .env와 외부 서비스는 읽지 않는다."""

from copy import deepcopy
import io
import json
from pathlib import Path
import unittest

from configuration_reference import (
    can_resume_configuration,
    ConfigurationError,
    llm_connection_plan,
    missing_service_settings,
    resolve_configuration,
    source_configuration_hash,
)


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationContractTests(unittest.TestCase):
    """최소 환경 설정, 우선순위, 비밀값 경계를 검증하는 사양 검사."""

    def setUp(self):
        """검사마다 비밀값이 없는 계약 파일의 새 사본을 읽는다."""
        self.defaults = json.loads((ROOT / "config/defaults.json").read_text())
        self.mapping = json.loads((ROOT / "config/env-map.json").read_text())

    def resolve(self, dotenv=None, process=None):
        """합성 환경 입력으로 순수 참조 함수를 호출한다."""
        return resolve_configuration(self.defaults, self.mapping, dotenv or {}, process)

    def assert_redacted(self, overrides, key):
        """오류가 키 이름을 알리되 입력한 값은 노출하지 않는지 확인한다."""
        with self.assertRaises(ConfigurationError) as caught:
            self.resolve(overrides)
        self.assertIn(key, str(caught.exception))
        self.assertNotIn(str(overrides[key]), str(caught.exception))

    def test_empty_environment_preserves_limits_and_permissions(self):
        """.env 없이 기본 한도와 비활성 권한을 유지한다."""
        config = self.resolve()
        self.assertEqual(config["execution"], self.defaults["execution"])
        self.assertEqual(config["season"], self.defaults["season"])
        self.assertEqual(config["secrets"], {})
        self.assertIs(config["external_transfer"]["document_text_allowed"], False)
        self.assertIs(config["external_transfer"]["image_allowed"], False)

    def test_minimal_key_subset_does_not_require_all_mapping_entries(self):
        """사용할 서비스의 연결 항목만 지정해도 고급 기본값을 사용할 수 있다."""
        config = self.resolve({
            "LLM_BACKEND": "ollama",
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "OLLAMA_TEXT_MODEL": "fixture-model",
            "NEIS_API_KEY": "fixture-neis",
            "TAVILY_API_KEY": "fixture-search",
        })
        self.assertEqual(config["execution"], self.defaults["execution"])
        self.assertEqual(missing_service_settings(config, "neis"), [])
        self.assertEqual(missing_service_settings(config, "tavily"), [])
        self.assertEqual(missing_service_settings(config, "ollama"), [])

    def test_process_environment_overrides_dotenv(self):
        """등록된 프로세스 값이 .env보다 우선한다."""
        config = self.resolve({"APP_PORT": "8601"}, {"APP_PORT": "8602"})
        self.assertEqual(config["app"]["port"], 8602)

    def test_blank_overrides_keep_previous_layer(self):
        """빈 상위 설정 때문에 유효한 하위 설정이 지워지지 않는다."""
        config = self.resolve(
            {"APP_PORT": " ", "NEIS_API_KEY": "fixture-neis"},
            {"APP_PORT": "", "NEIS_API_KEY": "  ", "OPENAI_API_KEY": None},
        )
        self.assertEqual(config["app"]["port"], self.defaults["app"]["port"])
        self.assertEqual(config["secrets"], {"neis_api_key": "fixture-neis"})

    def test_explicit_false_survives_true_default_and_lower_layer(self):
        """False를 미입력으로 취급하는 잘못된 기본값 복귀를 막는다."""
        config = self.resolve({"AUTO_OPEN_BROWSER": "false", "LANGSMITH_TRACING": True}, {"LANGSMITH_TRACING": False})
        self.assertIs(config["app"]["auto_open_browser"], False)
        self.assertIs(config["tracing"]["enabled"], False)

    def test_zero_is_parsed_without_silently_restoring_default(self):
        """0은 파싱 후 보존하며 한도 유효성은 실제 앱의 도메인 검사가 맡는다."""
        config = self.resolve({"HTTP_MAX_RETRIES": "0", "APP_PORT": 0})
        self.assertEqual(config["execution"]["http_max_retries"], 0)
        self.assertEqual(config["app"]["port"], 0)

    def test_string_whitespace_is_trimmed(self):
        """연결 정보 양끝의 불필요한 공백을 제거한다."""
        config = self.resolve({"OLLAMA_TEXT_MODEL": " fixture-model "})
        self.assertEqual(config["models"]["ollama_text_model"], "fixture-model")

    def test_enum_errors_do_not_repeat_input(self):
        """열거값 오류에 입력한 임의 문자열을 출력하지 않는다."""
        self.assert_redacted({"LLM_BACKEND": "fixture-sensitive-invalid"}, "LLM_BACKEND")

    def test_type_errors_do_not_repeat_input(self):
        """타입 오류도 민감할 수 있는 원래 값을 반사하지 않는다."""
        for key, value in [("APP_PORT", "fixture-sensitive-invalid"), ("AUTO_OPEN_BROWSER", "fixture-sensitive-invalid")]:
            with self.subTest(key=key):
                self.assert_redacted({key: value}, key)

    def test_boolean_is_not_integer_and_numeric_boolean_is_rejected(self):
        """Python bool의 int 상속과 느슨한 불리언 변환을 허용하지 않는다."""
        for overrides in [{"APP_PORT": True}, {"AUTO_OPEN_BROWSER": 1}, {"AUTO_OPEN_BROWSER": "1"}, {"APP_PORT": "1.0"}]:
            with self.subTest(key=next(iter(overrides))):
                with self.assertRaises(ConfigurationError):
                    self.resolve(overrides)

    def test_unknown_dotenv_key_is_rejected_with_name_only(self):
        """.env 오타를 무시하지 않고 값 없는 오류로 알린다."""
        with self.assertRaises(ConfigurationError) as caught:
            self.resolve({"NEIS_APY_KEY": "fixture-sensitive-value"})
        self.assertEqual(str(caught.exception), "UNKNOWN_ENV_KEY: NEIS_APY_KEY")

    def test_unrelated_process_environment_is_ignored(self):
        """일반 프로세스 환경 변수를 앱 설정 오류로 취급하지 않는다."""
        config = self.resolve(process={"PATH": "/fixture/bin", "UNRELATED_KEY": "fixture-value"})
        self.assertNotIn("UNRELATED_KEY", config)
        self.assertEqual(config["execution"], self.defaults["execution"])

    def test_inputs_and_nested_defaults_are_not_mutated(self):
        """여러 실행 사이에 설정과 비밀값이 기본 사본으로 새지 않는다."""
        dotenv = {"NEIS_API_KEY": "fixture-neis"}
        process = {"APP_PORT": "8601"}
        originals = deepcopy((self.defaults, self.mapping, dotenv, process))
        config = self.resolve(dotenv, process)
        config["execution"]["concurrency"] = 99
        self.assertEqual((self.defaults, self.mapping, dotenv, process), originals)
        self.assertNotIn("secrets", self.defaults)
        self.assertNotIn("fixture-neis", json.dumps(self.defaults))

    def test_every_secret_is_confined_to_returned_memory(self):
        """등록된 모든 키가 공유 기본값 대신 반환 객체의 비밀값 영역에만 놓인다."""
        synthetic = {
            key: f"fixture-secret-{index}"
            for index, (key, specification) in enumerate(self.mapping.items())
            if specification.get("secret", False)
        }
        config = self.resolve(synthetic)
        without_secrets = {key: value for key, value in config.items() if key != "secrets"}
        self.assertEqual(len(config["secrets"]), len(synthetic))
        for key, value in synthetic.items():
            path = self.mapping[key]["path"].split(".")[1]
            self.assertEqual(config["secrets"][path], value)
            self.assertNotIn(value, json.dumps(without_secrets))
            self.assertNotIn(value, json.dumps(self.defaults))

    def test_missing_services_do_not_block_configuration_creation(self):
        """미입력 서비스는 개별 진단하며 저장 기록 조회용 설정은 생성한다."""
        config = self.resolve()
        self.assertEqual(missing_service_settings(config, "neis"), ["NEIS_API_KEY"])
        self.assertEqual(missing_service_settings(config, "tavily"), ["TAVILY_API_KEY"])
        self.assertEqual(missing_service_settings(config, "openai"), ["OPENAI_API_KEY"])
        self.assertEqual(missing_service_settings(config, "ollama"), [])

    def test_openai_key_does_not_enable_external_document_transfer(self):
        """키 보관만으로 로컬 백엔드나 문서 전송 권한을 바꾸지 않는다."""
        config = self.resolve({"OPENAI_API_KEY": "fixture-openai", "OPENAI_FAST_MODEL": "fixture-model"})
        self.assertEqual(missing_service_settings(config, "openai"), [])
        self.assertEqual(config["external_transfer"], {"document_text_allowed": False, "image_allowed": False})
        self.assertEqual(config["models"]["openai_quality_model"], "fixture-model")
        self.assertEqual(llm_connection_plan(config)["backend"], "ollama")

    def test_openai_switch_needs_only_backend_and_key(self):
        """백엔드 선택과 키만으로 기본 OpenAI 모델의 연결 계획을 만든다."""
        config = self.resolve({"LLM_BACKEND": "openai", "OPENAI_API_KEY": "fixture-openai"})
        plan = llm_connection_plan(config)
        self.assertEqual(plan, {
            "backend": "openai",
            "text_model": "gpt-6.1-sol",
            "quality_model": "gpt-6.1-sol",
            "base_url": None,
            "missing_settings": [],
        })
        self.assertEqual(config["external_transfer"], {"document_text_allowed": True, "image_allowed": False})

    def test_switch_back_to_ollama_ignores_stored_openai_key(self):
        """같은 입력 사본에서 백엔드만 되돌리면 로컬 모델과 로컬 권한을 선택한다."""
        dotenv = {"LLM_BACKEND": "openai", "OPENAI_API_KEY": "fixture-openai"}
        remote = self.resolve(dotenv)
        dotenv["LLM_BACKEND"] = "ollama"
        local = self.resolve(dotenv)
        self.assertEqual(llm_connection_plan(remote)["backend"], "openai")
        self.assertEqual(llm_connection_plan(local)["backend"], "ollama")
        self.assertEqual(local["external_transfer"], {"document_text_allowed": False, "image_allowed": False})
        self.assertEqual(local["secrets"]["openai_api_key"], "fixture-openai")

    def test_process_selector_chooses_matching_profile_in_both_directions(self):
        """프로세스 선택이 최종 백엔드와 권한 프로필에 같은 우선순위로 적용된다."""
        for lower, higher, permission in [("ollama", "openai", True), ("openai", "ollama", False)]:
            with self.subTest(backend=higher):
                config = self.resolve({"LLM_BACKEND": lower}, {"LLM_BACKEND": higher})
                self.assertEqual(config["app"]["llm_backend"], higher)
                self.assertIs(config["external_transfer"]["document_text_allowed"], permission)

    def test_blank_process_selector_preserves_dotenv_choice_and_profile(self):
        """빈 프로세스 선택이 .env의 제공자와 권한을 초기화하지 않는다."""
        for blank in [None, "", "  "]:
            with self.subTest(blank=blank):
                config = self.resolve({"LLM_BACKEND": "openai"}, {"LLM_BACKEND": blank})
                self.assertEqual(config["app"]["llm_backend"], "openai")
                self.assertIs(config["external_transfer"]["document_text_allowed"], True)

    def test_invalid_default_or_process_selector_is_redacted(self):
        """기본값과 최상위 선택도 등록된 백엔드만 허용하고 원래 값은 숨긴다."""
        for backend in ["fixture-sensitive-backend", 7, []]:
            with self.subTest(value_type=type(backend).__name__):
                defaults = deepcopy(self.defaults)
                defaults["app"]["llm_backend"] = backend
                with self.assertRaises(ConfigurationError) as caught:
                    resolve_configuration(defaults, self.mapping, {})
                self.assertIn("LLM_BACKEND", str(caught.exception))
                self.assertNotIn("fixture-sensitive-backend", str(caught.exception))
        with self.assertRaises(ConfigurationError) as caught:
            self.resolve({"LLM_BACKEND": "openai"}, {"LLM_BACKEND": "fixture-sensitive-backend"})
        self.assertIn("LLM_BACKEND", str(caught.exception))
        self.assertNotIn("fixture-sensitive-backend", str(caught.exception))

    def test_explicit_permission_denial_overrides_openai_profile(self):
        """단일 선택의 기본 권한보다 사용자의 명시적 전송 금지가 우선한다."""
        config = self.resolve({"LLM_BACKEND": "openai", "ALLOW_EXTERNAL_DOCUMENT_TEXT": "false"})
        self.assertIs(config["external_transfer"]["document_text_allowed"], False)
        self.assertEqual(config["app"]["llm_backend"], "openai")

    def test_permission_process_override_and_blank_priority(self):
        """전송 권한도 빈 값 제외와 프로세스 우선순위를 유지한다."""
        denied = self.resolve(
            {"LLM_BACKEND": "openai", "ALLOW_EXTERNAL_DOCUMENT_TEXT": True},
            {"ALLOW_EXTERNAL_DOCUMENT_TEXT": False},
        )
        allowed = self.resolve(
            {"LLM_BACKEND": "openai", "ALLOW_EXTERNAL_DOCUMENT_TEXT": False},
            {"ALLOW_EXTERNAL_DOCUMENT_TEXT": "true"},
        )
        blank = self.resolve(
            {"LLM_BACKEND": "openai", "ALLOW_EXTERNAL_DOCUMENT_TEXT": False},
            {"ALLOW_EXTERNAL_DOCUMENT_TEXT": " "},
        )
        self.assertIs(denied["external_transfer"]["document_text_allowed"], False)
        self.assertIs(allowed["external_transfer"]["document_text_allowed"], True)
        self.assertIs(blank["external_transfer"]["document_text_allowed"], False)

    def test_openai_missing_key_does_not_fall_back_to_ollama(self):
        """키가 없으면 선택된 연결의 준비 부족을 알리고 로컬 모델로 바꾸지 않는다."""
        config = self.resolve({"LLM_BACKEND": "openai"})
        plan = llm_connection_plan(config)
        self.assertEqual(plan["backend"], "openai")
        self.assertEqual(plan["missing_settings"], ["OPENAI_API_KEY"])
        self.assertIsNone(plan["base_url"])

    def test_openai_plan_has_no_ollama_connection_dependency(self):
        """OpenAI 선택 시 없는 로컬 모델과 서버 주소를 연결 조건으로 검사하지 않는다."""
        config = self.resolve({"LLM_BACKEND": "openai", "OPENAI_API_KEY": "fixture-openai"})
        del config["models"]["ollama_base_url"]
        del config["models"]["ollama_text_model"]
        self.assertEqual(llm_connection_plan(config)["missing_settings"], [])
        self.assertIsNone(llm_connection_plan(config)["base_url"])

    def test_ollama_plan_does_not_require_openai_settings(self):
        """로컬 선택은 OpenAI 모델과 키를 준비 조건으로 사용하지 않는다."""
        config = self.resolve()
        del config["models"]["openai_fast_model"]
        del config["models"]["openai_quality_model"]
        plan = llm_connection_plan(config)
        self.assertEqual(plan["missing_settings"], [])
        self.assertEqual(plan["text_model"], config["models"]["ollama_text_model"])
        self.assertEqual(plan["quality_model"], plan["text_model"])
        self.assertEqual(plan["base_url"], config["models"]["ollama_base_url"])

    def test_fake_plan_requires_no_external_connection(self):
        """오프라인 합성 경로는 모델이나 키 없이 외부 전송을 비활성화한다."""
        config = self.resolve({"LLM_BACKEND": "fake"})
        config["models"] = {}
        self.assertEqual(llm_connection_plan(config), {
            "backend": "fake", "text_model": None, "quality_model": None,
            "base_url": None, "missing_settings": [],
        })
        self.assertEqual(config["external_transfer"], {"document_text_allowed": False, "image_allowed": False})

    def test_connection_plan_never_contains_secret_values(self):
        """연결 계획은 로그에 쓸 수 있는 키 이름만 포함하고 인증값은 반환하지 않는다."""
        for backend in ["ollama", "openai", "fake"]:
            with self.subTest(backend=backend):
                config = self.resolve({
                    "LLM_BACKEND": backend,
                    "OPENAI_API_KEY": "fixture-sensitive-openai",
                    "NEIS_API_KEY": "fixture-sensitive-neis",
                    "TAVILY_API_KEY": "fixture-sensitive-search",
                })
                before = deepcopy(config)
                encoded = json.dumps(llm_connection_plan(config))
                for value in config["secrets"].values():
                    self.assertNotIn(value, encoded)
                self.assertEqual(config, before)

    def test_invalid_profiles_are_rejected_without_echoing_values(self):
        """프로필은 등록된 세 제공자와 두 불리언 권한 이외의 주입을 거절한다."""
        invalid_profiles = [None, {}, [], {"fixture-sensitive-provider": {}}, {
            "ollama": {"document_text_allowed": False, "image_allowed": False},
            "openai": {"document_text_allowed": True, "image_allowed": False},
        }]
        for profiles in invalid_profiles:
            with self.subTest(profile_type=type(profiles).__name__):
                defaults = deepcopy(self.defaults)
                defaults["llm_profiles"] = profiles
                with self.assertRaises(ConfigurationError) as caught:
                    resolve_configuration(defaults, self.mapping, {})
                self.assertNotIn("fixture-sensitive-provider", str(caught.exception))

    def test_profile_rejects_secret_injection_and_nonboolean_permissions(self):
        """권한 프로필을 비밀값이나 추가 설정의 우회 입력 경로로 사용하지 못한다."""
        invalid = [
            {"document_text_allowed": True, "image_allowed": False, "secrets": {"openai_api_key": "fixture-sensitive-key"}},
            {"document_text_allowed": "fixture-sensitive-value", "image_allowed": False},
            {"document_text_allowed": 1, "image_allowed": False},
            {"document_text_allowed": True},
            {"document_text_allowed": True, "image_allowed": False, "models": {}},
            None,
        ]
        for profile in invalid:
            with self.subTest(profile_type=type(profile).__name__):
                defaults = deepcopy(self.defaults)
                defaults["llm_profiles"]["openai"] = profile
                with self.assertRaises(ConfigurationError) as caught:
                    resolve_configuration(defaults, self.mapping, {})
                self.assertNotIn("fixture-sensitive", str(caught.exception))

    def test_selected_profile_cannot_mutate_default_or_other_resolved_config(self):
        """프로필 적용 후의 권한 변경이 기본값이나 별도 실행에 전파되지 않는다."""
        original = deepcopy(self.defaults)
        first = self.resolve({"LLM_BACKEND": "openai"})
        first["external_transfer"]["document_text_allowed"] = False
        first["llm_profiles"]["openai"]["document_text_allowed"] = False
        second = self.resolve({"LLM_BACKEND": "openai"})
        self.assertIs(second["external_transfer"]["document_text_allowed"], True)
        self.assertEqual(self.defaults, original)

    def test_connection_plan_rejects_unknown_backend_without_echoing(self):
        """직접 전달한 미지원 백엔드도 입력값 없는 오류로 거절한다."""
        config = self.resolve()
        for backend in ["fixture-sensitive-backend", None, []]:
            with self.subTest(value_type=type(backend).__name__):
                config["app"]["llm_backend"] = backend
                with self.assertRaises(ConfigurationError) as caught:
                    llm_connection_plan(config)
                self.assertEqual(str(caught.exception), "VALUE_NOT_ALLOWED: LLM_BACKEND")

    def test_explicit_quality_model_is_kept(self):
        """선택적인 품질 모델이 있으면 기본 모델로 덮어쓰지 않는다."""
        config = self.resolve({"OPENAI_FAST_MODEL": "fixture-fast", "OPENAI_QUALITY_MODEL": "fixture-quality"})
        self.assertEqual(config["models"]["openai_quality_model"], "fixture-quality")

    def test_ollama_requires_only_text_connection_settings(self):
        """비활성 이미지 모델은 텍스트 수집의 필수 준비 항목이 아니다."""
        config = self.resolve()
        config["models"]["ollama_text_model"] = ""
        config["models"]["ollama_vision_model"] = ""
        self.assertEqual(missing_service_settings(config, "ollama"), ["OLLAMA_TEXT_MODEL"])

    def test_all_nonsecret_mapping_paths_exist_in_defaults(self):
        """선택적인 고급 환경 변수도 기존 기본값 경로만 덮어쓴다."""
        config = self.resolve()
        for key, specification in self.mapping.items():
            if specification.get("secret", False):
                self.assertTrue(specification["path"].startswith("secrets."), key)
                continue
            current = config
            for part in specification["path"].split("."):
                self.assertIn(part, current, key)
                current = current[part]

    def test_secrets_cannot_be_placed_in_nonsecret_defaults(self):
        """공유 기본 설정과 반환 비밀값 영역의 경계를 강제한다."""
        invalid = deepcopy(self.mapping)
        invalid["NEIS_API_KEY"]["path"] = "app.mode"
        with self.assertRaises(ConfigurationError):
            resolve_configuration(self.defaults, invalid, {})
        defaults = deepcopy(self.defaults)
        defaults["secrets"] = {"neis_api_key": "fixture-key"}
        with self.assertRaises(ConfigurationError) as caught:
            resolve_configuration(defaults, self.mapping, {})
        self.assertNotIn("fixture-key", str(caught.exception))

    def test_unknown_service_fails_without_echoing_argument(self):
        """서비스 오타는 입력값을 출력하지 않는 명시적 오류다."""
        with self.assertRaises(ConfigurationError) as caught:
            missing_service_settings(self.resolve(), "fixture-sensitive-service")
        self.assertEqual(str(caught.exception), "UNKNOWN_SERVICE")

    def test_unchanged_source_configuration_allows_resume(self):
        """같은 수집 설정의 해시만 보존해도 변경 없는 작업을 재개할 수 있다."""
        config = self.resolve()
        digest = source_configuration_hash(config)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertTrue(can_resume_configuration({"source_config_hash": digest}, config))

    def test_changed_backend_or_active_model_blocks_resume(self):
        """다른 제공자·활성 모델·서버 주소로 기존 작업을 이어 가지 못한다."""
        local = self.resolve()
        local_scope = {"source_config_hash": source_configuration_hash(local)}
        changed = self.resolve({"LLM_BACKEND": "openai", "OPENAI_API_KEY": "fixture-key"})
        self.assertFalse(can_resume_configuration(local_scope, changed))
        for key, value in [("ollama_text_model", "fixture-new-model"), ("ollama_base_url", "http://127.0.0.1:11435")]:
            with self.subTest(key=key):
                changed = deepcopy(local)
                changed["models"][key] = value
                self.assertFalse(can_resume_configuration(local_scope, changed))
        remote = self.resolve({"LLM_BACKEND": "openai"})
        remote_scope = {"source_config_hash": source_configuration_hash(remote)}
        for key in ["openai_fast_model", "openai_quality_model"]:
            with self.subTest(key=key):
                changed = deepcopy(remote)
                changed["models"][key] = "fixture-new-model"
                self.assertFalse(can_resume_configuration(remote_scope, changed))

    def test_changed_source_permissions_tools_or_publication_blocks_resume(self):
        """수집 범위와 게시 판단에 영향을 주는 허용 필드 변경을 감지한다."""
        config = self.resolve()
        scope = {"source_config_hash": source_configuration_hash(config)}
        changes = [
            ("app", "mode", "demo"),
            ("app", "search_provider", "fixture-provider"),
            ("documents", "browser_fallback_enabled", True),
            ("documents", "hwp_preview_enabled", True),
            ("search", "domain_filter_enabled", True),
            ("external_transfer", "document_text_allowed", True),
            ("external_transfer", "image_allowed", True),
            ("publication", "policy_version", "fixture-policy"),
            ("publication", "auto_rule_ids", ["fixture-rule"]),
            ("models", "image_candidates_enabled", True),
        ]
        for section, key, value in changes:
            with self.subTest(path=f"{section}.{key}"):
                changed = deepcopy(config)
                changed[section][key] = value
                self.assertFalse(can_resume_configuration(scope, changed))

    def test_secret_rotation_and_missing_key_do_not_change_source_hash(self):
        """인증값 교체와 누락은 해시에서 제외하고 호출 준비 점검에만 반영한다."""
        original = self.resolve({"LLM_BACKEND": "openai", "OPENAI_API_KEY": "fixture-original"})
        scope = {"source_config_hash": source_configuration_hash(original)}
        for secrets in [{}, {"openai_api_key": "fixture-rotated", "neis_api_key": "fixture-neis", "tavily_api_key": "fixture-tavily"}]:
            with self.subTest(keys=list(secrets)):
                changed = deepcopy(original)
                changed["secrets"] = secrets
                self.assertTrue(can_resume_configuration(scope, changed))
        missing = deepcopy(original)
        missing["secrets"] = {}
        self.assertEqual(llm_connection_plan(missing)["missing_settings"], ["OPENAI_API_KEY"])

    def test_inactive_openai_configuration_does_not_change_local_hash(self):
        """로컬 작업은 보관된 OpenAI 키·모델과 비선택 프로필 변경의 영향을 받지 않는다."""
        config = self.resolve()
        scope = {"source_config_hash": source_configuration_hash(config)}
        changed = deepcopy(config)
        changed["secrets"]["openai_api_key"] = "fixture-new-key"
        changed["models"]["openai_fast_model"] = "fixture-new-fast"
        changed["models"]["openai_quality_model"] = "fixture-new-quality"
        changed["llm_profiles"]["openai"]["document_text_allowed"] = False
        self.assertTrue(can_resume_configuration(scope, changed))

    def test_inactive_ollama_configuration_does_not_change_openai_hash(self):
        """OpenAI 작업의 해시에 비선택 로컬 서버·텍스트/이미지 모델을 넣지 않는다."""
        config = self.resolve({"LLM_BACKEND": "openai"})
        scope = {"source_config_hash": source_configuration_hash(config)}
        changed = deepcopy(config)
        changed["models"]["ollama_base_url"] = "http://127.0.0.1:11435"
        changed["models"]["ollama_text_model"] = "fixture-new-local"
        changed["models"]["ollama_vision_model"] = "fixture-new-vision"
        self.assertTrue(can_resume_configuration(scope, changed))

    def test_ui_tracing_season_and_limits_do_not_change_source_hash(self):
        """화면·추적과 별도 보존하는 시즌·실행 한도는 수집 설정 해시와 분리한다."""
        config = self.resolve()
        scope = {"source_config_hash": source_configuration_hash(config)}
        changed = deepcopy(config)
        changed["app"].update({"host": "localhost", "port": 8601, "auto_open_browser": False, "poll_interval_ms": 500})
        changed["tracing"] = {"enabled": True, "project": "fixture-tracing"}
        changed["season"]["academic_year"] += 1
        changed["execution"]["item_max_search_calls"] += 1
        self.assertTrue(can_resume_configuration(scope, changed))

    def test_unknown_fields_are_excluded_by_hash_whitelist(self):
        """미등록 설정이나 문서 도구의 임의 필드를 전체 덤프처럼 해시하지 않는다."""
        config = self.resolve()
        scope = {"source_config_hash": source_configuration_hash(config)}
        changed = deepcopy(config)
        changed["fixture-private-value"] = "fixture-sensitive"
        changed["documents"]["fixture-private-value"] = "fixture-sensitive"
        changed["publication"]["fixture-private-value"] = "fixture-sensitive"
        self.assertTrue(can_resume_configuration(scope, changed))

    def test_vision_model_hash_depends_on_active_local_image_path(self):
        """이미지 후보가 활성인 Ollama 경로에서만 이미지 모델 변경을 감지한다."""
        for backend, images, should_change in [("ollama", False, False), ("ollama", True, True), ("openai", True, False)]:
            with self.subTest(backend=backend, images=images):
                config = self.resolve({"LLM_BACKEND": backend, "ENABLE_IMAGE_CANDIDATES": images})
                scope = {"source_config_hash": source_configuration_hash(config)}
                changed = deepcopy(config)
                changed["models"]["ollama_vision_model"] = "fixture-new-vision"
                self.assertIs(can_resume_configuration(scope, changed), not should_change)

    def test_missing_legacy_or_wrong_hash_refuses_resume(self):
        """해시가 없는 이전 기록과 일치하지 않는 해시는 재개를 허용하지 않는다."""
        config = self.resolve()
        for scope in [None, {}, {"source_config_hash": None}, {"source_config_hash": "demo-source-config"}, {"source_config_hash": "0" * 64}]:
            with self.subTest(scope=scope):
                self.assertFalse(can_resume_configuration(scope, config))

    def test_invalid_hash_projection_fields_are_redacted(self):
        """해시 대상 타입 오류를 조용히 변환하거나 민감한 값과 함께 출력하지 않는다."""
        for section, key, value in [
            ("external_transfer", "image_allowed", "fixture-sensitive"),
            ("publication", "auto_rule_ids", ["fixture-rule", {"fixture-sensitive": True}]),
            ("models", "ollama_text_model", {"fixture-sensitive": True}),
        ]:
            with self.subTest(path=f"{section}.{key}"):
                config = self.resolve()
                config[section][key] = value
                with self.assertRaises(ConfigurationError) as caught:
                    source_configuration_hash(config)
                self.assertNotIn("fixture-sensitive", str(caught.exception))

    def test_source_hash_is_canonical_and_does_not_mutate_configuration(self):
        """사전 삽입 순서와 무관한 해시를 만들고 입력 설정은 보존한다."""
        config = self.resolve()
        original = deepcopy(config)
        reversed_config = {key: dict(reversed(list(value.items()))) if isinstance(value, dict) else value for key, value in reversed(list(config.items()))}
        self.assertEqual(source_configuration_hash(config), source_configuration_hash(reversed_config))
        self.assertEqual(config, original)


def main() -> None:
    """사양 검사 결과를 기계가 읽을 수 있는 JSON 한 줄로 출력한다."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ConfigurationContractTests)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    summary = {
        "contract_version": "4.0",
        "configuration_cases": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "status": "passed" if result.wasSuccessful() else "failed",
        "scope": "specification reference with synthetic inputs; no real .env, process environment, or service calls",
    }
    if not result.wasSuccessful():
        summary["details"] = output.getvalue()
    print(json.dumps(summary, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
