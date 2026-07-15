"""Server option vocabulary shared by config and process preparation."""

from __future__ import annotations


MLX_LM_OPTION_TYPES = {
    "draft_model": "string",
    "prompt_cache_size": "integer",
    "prompt_concurrency": "integer",
    "pipeline": "boolean",
    "temp": "number",
    "top_p": "number",
    "top_k": "integer",
}

OPTIQ_OPTION_TYPES = {
    **MLX_LM_OPTION_TYPES,
    "adapter": "string array",
    "allow_model_switch": "boolean",
    "anthropic": "boolean",
    "idle_timeout": "number",
    "kv_bits": "integer",
    "kv_config": "string",
    "kv_group_size": "integer",
    "max_context": "integer",
    "mtp": "boolean",
    "quantized_kv_start": "integer",
}

OPTION_TYPES_BY_SERVER_TYPE = {
    "mlx_lm": MLX_LM_OPTION_TYPES,
    "optiq": OPTIQ_OPTION_TYPES,
}
