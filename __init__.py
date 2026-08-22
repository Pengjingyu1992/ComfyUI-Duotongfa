"""ComfyUI-Duotongfa package marker.

Duotongfa is a companion runtime, not a visual inference node. Keeping the
node mappings empty prevents it from inventing or replacing native nodes.
"""

try:
    from .duotongfa_runtime import RUNTIME_VERSION
except ImportError:  # ComfyUI and pytest may load a hyphenated folder as a module.
    from duotongfa_runtime import RUNTIME_VERSION

__version__ = RUNTIME_VERSION
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "__version__"]
