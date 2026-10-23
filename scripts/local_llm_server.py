"""Serve a local Hugging Face chat model behind an OpenAI-compatible endpoint.

VENTHORA (like the author's Theorem/Verity/Horizon agents) talks to any
`{MODEL_ENDPOINT}/v1/chat/completions`. This script makes a model already in the local
HF cache available there, so evidence reading runs fully offline on the laptop GPU.

Run with a Python that has torch + transformers (it does not need VENTHORA's venv):

    python scripts/local_llm_server.py --model Qwen/Qwen2.5-0.5B-Instruct --port 8001

then set MODEL_ENDPOINT=http://127.0.0.1:8001 and MODEL_NAME=Qwen/Qwen2.5-0.5B-Instruct.
Binds to 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load(model_id: str, offline: bool):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(model_id, local_files_only=offline)
    model = AutoModelForCausalLM.from_pretrained(
        model_id, local_files_only=offline, dtype=torch.float16 if device == "cuda" else torch.float32)
    model.to(device).eval()
    return tok, model, device


def make_handler(model_id, tok, model, device):
    import torch

    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code: int, body: dict):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path.rstrip("/") in ("/v1/models", "/health"):
                self._send(200, {"object": "list", "data": [{"id": model_id, "object": "model", "device": device}]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.rstrip("/").endswith("/chat/completions"):
                return self._send(404, {"error": "not found"})
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            max_new = int(req.get("max_tokens", 256))
            temperature = float(req.get("temperature", 0.0))
            prompt = tok.apply_chat_template(req["messages"], tokenize=False, add_generation_prompt=True)
            inputs = tok(prompt, return_tensors="pt").to(device)
            started = time.time()
            with lock, torch.no_grad():
                out = model.generate(**inputs, max_new_tokens=max_new, do_sample=temperature > 0,
                                     temperature=temperature if temperature > 0 else None,
                                     top_p=None, top_k=None, pad_token_id=tok.eos_token_id)
            new_tokens = out[0][inputs["input_ids"].shape[1]:]
            text = tok.decode(new_tokens, skip_special_tokens=True)
            self._send(200, {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(started),
                "model": model_id,
                "choices": [{"index": 0, "finish_reason": "stop" if len(new_tokens) < max_new else "length",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": int(inputs["input_ids"].shape[1]), "completion_tokens": int(len(new_tokens)),
                          "latency_s": round(time.time() - started, 3)}})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--allow-download", action="store_true", help="fetch the model if it is not cached")
    args = ap.parse_args()
    tok, model, device = load(args.model, offline=not args.allow_download)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.model, tok, model, device))
    print(f"serving {args.model} on http://127.0.0.1:{args.port}/v1/chat/completions ({device})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
