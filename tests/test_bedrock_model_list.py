"""Amazon Bedrock: the model list is read live from the account's control plane with the Bedrock API key; inference
profiles of text models first, then the on-demand text models; inactive or non-text models are left out."""
import asyncio

import httpx

from venthora import ai_settings
from venthora.evidence.llm import BedrockModel

FM = {"modelSummaries": [
    {"modelId": "anthropic.claude-haiku-4-5-20251001-v1:0", "modelName": "Claude Haiku 4.5", "inputModalities": ["TEXT", "IMAGE"],
     "inferenceTypesSupported": ["INFERENCE_PROFILE"], "modelLifecycle": {"status": "ACTIVE"}},
    {"modelId": "amazon.nova-lite-v1:0", "modelName": "Nova Lite", "inputModalities": ["TEXT"],
     "inferenceTypesSupported": ["ON_DEMAND", "INFERENCE_PROFILE"], "modelLifecycle": {"status": "ACTIVE"}},
    {"modelId": "old.model-v1", "modelName": "Old", "inputModalities": ["TEXT"], "inferenceTypesSupported": ["ON_DEMAND"],
     "modelLifecycle": {"status": "LEGACY"}},
]}
IP = {"inferenceProfileSummaries": [
    {"inferenceProfileId": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "inferenceProfileName": "US Claude Haiku 4.5", "status": "ACTIVE"},
    {"inferenceProfileId": "us.stability.sd3-v1:0", "inferenceProfileName": "US SD3 (image)", "status": "ACTIVE"},
]}


def test_the_bedrock_list_is_read_live_with_the_api_key_from_the_regions_control_plane():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.host, req.url.path, req.headers.get("authorization")))
        return httpx.Response(200, json=FM if req.url.path.endswith("foundation-models") else IP)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            m = BedrockModel(api_key="k" * 20, model="", endpoint="https://bedrock-runtime.ap-southeast-1.amazonaws.com", client=client)
            return await ai_settings.list_models_labeled(m)

    ids, labels = asyncio.run(go())
    assert {h for h, _, _ in seen} == {"bedrock.ap-southeast-1.amazonaws.com"}
    assert all(a == "Bearer " + "k" * 20 for _, _, a in seen)
    assert ids == ["us.anthropic.claude-haiku-4-5-20251001-v1:0", "amazon.nova-lite-v1:0"]
    assert labels["amazon.nova-lite-v1:0"] == "Nova Lite"
    assert "bedrock" in ai_settings.PROVIDERS and "ap-southeast-1" in ai_settings.PROVIDERS["bedrock"]["regions"]
