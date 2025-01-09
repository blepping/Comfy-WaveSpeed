import contextlib
import unittest
import torch

from comfy import model_management

from . import first_block_cache


class ApplyFBCacheOnModel:

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", ),
                "object_to_patch": (
                    "STRING",
                    {
                        "default": "diffusion_model",
                    },
                ),
                "residual_diff_threshold": (
                    "FLOAT",
                    {
                        "default": 0.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.001,
                        "tooltip": "Controls the tolerance for caching with lower values being more strict. Setting this to 0 disables the FBCache effect.",
                    },
                ),
            }
        }

    RETURN_TYPES = ("MODEL", )
    FUNCTION = "patch"

    CATEGORY = "wavespeed"

    def patch(
        self,
        model,
        object_to_patch,
        residual_diff_threshold,
        max_consecutive_cache_hits=None,
        start_percent=None,
        end_percent=None,
    ):
        if residual_diff_threshold <= 0:
            return (model,)
        prev_timestep = None
        current_timestep = None
        consecutive_cache_hits = 0

        model = model.clone()
        diffusion_model = model.get_model_object(object_to_patch)

        double_blocks_name = None
        single_blocks_name = None
        if hasattr(diffusion_model, "transformer_blocks"):
            double_blocks_name = "transformer_blocks"
        elif hasattr(diffusion_model, "double_blocks"):
            double_blocks_name = "double_blocks"
        elif hasattr(diffusion_model, "joint_blocks"):
            double_blocks_name = "joint_blocks"
        else:
            raise ValueError("No transformer blocks found")

        if hasattr(diffusion_model, "single_blocks"):
            single_blocks_name = "single_blocks"

        if start_percent is not None or end_percent is not None:
            model_sampling = model.get_model_object("model_sampling")
            start_sigma, end_sigma = (
                None if pct is None else float(model_sampling.percent_to_sigma(pct))
                for pct in (start_percent, end_percent)
            )
            del model_sampling
        else:
            start_sigma = end_sigma = None

        def validate_use_cache(use_cached):
            nonlocal consecutive_cache_hits
            use_cached = use_cached and (start_sigma is None or current_timestep <= start_sigma)
            use_cached = use_cached and (end_sigma is None or current_timestep >= end_sigma)
            use_cached = use_cached and (max_consecutive_cache_hits is None or consecutive_cache_hits < max_consecutive_cache_hits)
            consecutive_cache_hits = consecutive_cache_hits + 1 if use_cached else 0
            return use_cached

        cached_transformer_blocks = torch.nn.ModuleList([
            first_block_cache.CachedTransformerBlocks(
                None if double_blocks_name is None else getattr(
                    diffusion_model, double_blocks_name),
                None if single_blocks_name is None else getattr(
                    diffusion_model, single_blocks_name),
                residual_diff_threshold=residual_diff_threshold,
                validate_can_use_cache_function=validate_use_cache,
                cat_hidden_states_first=diffusion_model.__class__.__name__ ==
                "HunyuanVideo",
                return_hidden_states_only=diffusion_model.__class__.__name__ ==
                "LTXVModel",
                clone_original_hidden_states=diffusion_model.__class__.__name__
                == "LTXVModel",
                return_hidden_states_first=diffusion_model.__class__.__name__
                != "OpenAISignatureMMDITWrapper",
                accept_hidden_states_first=diffusion_model.__class__.__name__
                != "OpenAISignatureMMDITWrapper",
            )
        ])
        dummy_single_transformer_blocks = torch.nn.ModuleList()

        def model_unet_function_wrapper(model_function, kwargs):
            nonlocal prev_timestep, current_timestep, consecutive_cache_hits

            try:
                input = kwargs["input"]
                timestep = kwargs["timestep"]
                c = kwargs["c"]
                current_timestep = t = timestep[0].item()

                if prev_timestep is None or t >= prev_timestep:
                    prev_timestep = t
                    consecutive_cache_hits = 0
                    first_block_cache.set_current_cache_context(
                        first_block_cache.create_cache_context())

                with unittest.mock.patch.object(
                        diffusion_model,
                        double_blocks_name,
                        cached_transformer_blocks,
                ), unittest.mock.patch.object(
                        diffusion_model,
                        single_blocks_name,
                        dummy_single_transformer_blocks,
                ) if single_blocks_name is not None else contextlib.nullcontext():
                    return model_function(input, timestep, **c)
            except model_management.InterruptProcessingException as exc:
                prev_timestep = None
                raise exc from None

        model.set_model_unet_function_wrapper(model_unet_function_wrapper)
        return (model, )


class ApplyFBCacheOnModelAdvanced(ApplyFBCacheOnModel):
    @classmethod
    def INPUT_TYPES(cls):
        result = super().INPUT_TYPES()
        result["required"] |= {
            "start_percent": (
                "FLOAT", {
                    "default": 0.0,
                    "step": 0.01,
                    "max": 1.0,
                    "min": 0.0,
                    "tooltip": "Start time as a percentage of sampling where the FBCache effect can apply.",
                },
            ),
            "end_percent": (
                "FLOAT", {
                    "default": 1.0,
                    "step": 0.01,
                    "max": 1.0,
                    "min": 0.0,
                    "tooltip": "End time as a percentage of sampling where the FBCache effect can apply.",
                }
            ),
            "max_consecutive_cache_hits": (
                "INT", {
                    "default": 2,
                    "min": 1,
                    "tooltip": "Allows putting a limit on how many cached results can be used in a row. For example, setting this to 1 will mean there will be at least one full model call after each cached result.",
                },
            ),
        }
        return result
