"""NVIDIA NIM provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

nvidia = ProviderProfile(
    name="nvidia",
    aliases=("nvidia-nim",),
    env_vars=("NVIDIA_API_KEY", "NVIDIA_API_KEY_2", "NVIDIA_API_KEY_3", "NVIDIA_API_KEY_4"),
    display_name="NVIDIA NIM",
    description="NVIDIA NIM — accelerated inference",
    signup_url="https://build.nvidia.com/",
    fallback_models=(
        "z-ai/glm-5.2",
        "minimaxai/minimax-m3",
        "deepseek-ai/deepseek-v4-flash-0731",
        "stepfun-ai/step-3.7-flash",
        "meta/llama-3.1-8b-instruct",
        "meta/llama-3.3-70b-instruct",
    ),
    base_url="https://integrate.api.nvidia.com/v1",
    default_max_tokens=16384,
    supports_vision=True,
    default_aux_model="meta/llama-3.1-8b-instruct",
)

register_provider(nvidia)

