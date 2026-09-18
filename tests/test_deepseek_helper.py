from app.services.deepseek_helper import (
    build_deepseek_model,
    is_deepseek_model,
    is_deepseek_v4_model,
)


def test_current_deepseek_flash_is_v4_family():
    assert is_deepseek_v4_model("deepseek-flash")
    assert is_deepseek_v4_model(" DEEPSEEK-FLASH ")


def test_current_deepseek_flash_gets_v4_profile():
    model = build_deepseek_model(
        api_key="test-key",
        model_name="deepseek-flash",
    )

    assert model.profile["supports_thinking"] is True
    assert model.profile["openai_chat_thinking_field"] == "reasoning_content"
    assert model.profile["openai_chat_send_back_thinking_parts"] == "field"
    assert model.profile["openai_supports_tool_choice_required"] is False


def test_legacy_v4_aliases_remain_supported():
    assert is_deepseek_v4_model("deepseek-v4-flash")
    assert is_deepseek_v4_model("deepseek-v4-flash-vision-exp")
    assert is_deepseek_v4_model("deepseek-v4-pro")


def test_legacy_non_v4_models_are_not_v4_family():
    assert not is_deepseek_v4_model("deepseek-chat")
    assert not is_deepseek_v4_model("deepseek-reasoner")
    assert is_deepseek_model("deepseek-chat")
    assert is_deepseek_model("deepseek-reasoner")
