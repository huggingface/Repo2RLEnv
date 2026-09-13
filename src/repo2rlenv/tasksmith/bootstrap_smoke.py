"""Small real HF operations, executed only in remote CPU/GPU sandboxes."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path


def check(repository: str, resource: str) -> dict:
    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("HF repository smoke checks execute remotely only")
    if resource not in {"cpu", "gpu"}:
        raise ValueError(f"Unsupported smoke resource: {resource}")
    if repository not in {"transformers", "accelerate", "trl", "diffusers", "peft", "tokenizers"}:
        raise ValueError(f"Unsupported smoke repository: {repository}")
    module = importlib.import_module(repository)
    origin = Path(module.__file__).resolve()
    if not origin.is_relative_to(Path("/workspace")):
        raise RuntimeError(f"Smoke imported {repository} outside its pinned checkout: {origin}")
    import torch

    device = "cuda" if resource == "gpu" else "cpu"
    if resource == "gpu":
        assert torch.cuda.is_available(), "A CUDA build alone is not GPU readiness"
    torch.manual_seed(0)
    torch.set_num_threads(2)
    checks = []
    if repository == "transformers":
        from transformers import BertConfig, BertModel

        model = BertModel(
            BertConfig(
                vocab_size=32,
                hidden_size=16,
                num_hidden_layers=1,
                num_attention_heads=2,
                intermediate_size=32,
            )
        ).to(device)
        output = model(torch.tensor([[1, 2, 3]], device=device)).last_hidden_state
        assert output.shape == (1, 3, 16) and torch.isfinite(output).all()
        output.square().mean().backward()
        assert any(p.grad is not None and torch.isfinite(p.grad).all() for p in model.parameters())
        checks.append("tiny BERT forward and backward without downloaded weights")
    elif repository == "accelerate":
        from accelerate import Accelerator

        accelerator = Accelerator(cpu=resource == "cpu", mixed_precision="no")
        assert accelerator.device.type == device
        model = torch.nn.Linear(4, 2)
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
        model, optimizer = accelerator.prepare(model, optimizer)
        before = next(model.parameters()).detach().clone()
        loss = model(torch.ones(2, 4, device=accelerator.device)).square().mean()
        accelerator.backward(loss)
        optimizer.step()
        assert torch.isfinite(loss) and not torch.equal(before, next(model.parameters()))
        checks.append("Accelerator device placement and an optimizer step")
    elif repository == "trl":
        from trl.trainer.utils import entropy_from_logits, selective_log_softmax

        logits = torch.randn(2, 3, 7, device=device, requires_grad=True)
        ids = torch.tensor([[0, 1, 2], [2, 3, 4]], device=device)
        selected = selective_log_softmax(logits, ids)
        expected = logits.log_softmax(-1).gather(-1, ids.unsqueeze(-1)).squeeze(-1)
        torch.testing.assert_close(selected, expected)
        entropy = entropy_from_logits(logits)
        torch.testing.assert_close(entropy, -(logits.softmax(-1) * logits.log_softmax(-1)).sum(-1))
        selected.sum().backward()
        assert torch.isfinite(logits.grad).all()
        checks.append("TRL log-probability and entropy numerics with autograd")
    elif repository == "diffusers":
        from diffusers import DDPMScheduler, UNet2DModel

        model = UNet2DModel(
            sample_size=8,
            in_channels=3,
            out_channels=3,
            layers_per_block=1,
            block_out_channels=(16,),
            down_block_types=("DownBlock2D",),
            up_block_types=("UpBlock2D",),
            norm_num_groups=4,
        ).to(device)
        sample = torch.randn(1, 3, 8, 8, device=device)
        output = model(sample, timestep=1).sample
        assert output.shape == sample.shape and torch.isfinite(output).all()
        scheduler = DDPMScheduler(num_train_timesteps=10)
        previous = scheduler.step(output.detach(), 1, sample).prev_sample
        assert previous.device == sample.device and torch.isfinite(previous).all()
        output.square().mean().backward()
        checks.append("tiny UNet forward/backward and DDPM scheduler step")
    elif repository == "peft":
        from peft import LoraConfig, get_peft_model
        from transformers import BertConfig, BertModel

        base = BertModel(
            BertConfig(
                vocab_size=32,
                hidden_size=16,
                num_hidden_layers=1,
                num_attention_heads=2,
                intermediate_size=32,
            )
        )
        model = get_peft_model(base, LoraConfig(r=2, target_modules=["query", "value"])).to(device)
        output = model(torch.tensor([[1, 2, 3]], device=device)).last_hidden_state
        output.square().mean().backward()
        gradients = [p.grad for n, p in model.named_parameters() if "lora_" in n]
        assert gradients and all(g is not None and torch.isfinite(g).all() for g in gradients)
        model.merge_and_unload()
        checks.append("LoRA injection, adapter gradients and merge on a tiny BERT")
    elif repository == "tokenizers":
        from tokenizers import Tokenizer, models, pre_tokenizers

        tokenizer = Tokenizer(
            models.WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]")
        )
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        assert tokenizer.encode("hello world").ids == [1, 2]
        assert Tokenizer.from_str(tokenizer.to_str()).decode([1, 2]) == "hello world"
        checks.append("source-built Rust tokenizer encode/decode and serialization on CPU")
    else:
        raise ValueError(f"Unsupported smoke repository: {repository}")
    if resource == "gpu":
        # Tokenizers is CPU-only; this separate operation checks the host GPU.
        value = torch.eye(4, device="cuda")
        torch.testing.assert_close(value @ value, value)
        torch.cuda.synchronize()
    return {
        "repository": repository,
        "resource": resource,
        "module_origin": str(origin),
        "repository_version": getattr(module, "__version__", None),
        "checks": checks,
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0) if resource == "gpu" else None,
        "repository_uses_gpu": resource == "gpu" and repository != "tokenizers",
    }


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1], sys.argv[2]), indent=2))
