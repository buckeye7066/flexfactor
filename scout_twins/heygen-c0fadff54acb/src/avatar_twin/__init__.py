"""Independent public-behavior avatar-video studio retained by Program Scout."""

from .library import AvatarLibrary, TemplateStore, VoiceCatalog
from .live import HttpLiveAvatarProvider, LiveSession, LiveSessionStore
from .models import AvatarProfile, Background, BrandKit, ConsentRecord, Scene, VideoProject, VoiceProfile
from .planning import HttpVideoAgentPlanner, ProjectWorkflow, VideoPlanner
from .renderer import RenderEngine
from .studio import BrandKitStore, StudioProjectStore

__all__ = [
    "AvatarProfile",
    "AvatarLibrary",
    "Background",
    "BrandKit",
    "BrandKitStore",
    "ConsentRecord",
    "Scene",
    "StudioProjectStore",
    "TemplateStore",
    "VideoProject",
    "VoiceCatalog",
    "VoiceProfile",
    "HttpVideoAgentPlanner",
    "HttpLiveAvatarProvider",
    "LiveSession",
    "LiveSessionStore",
    "ProjectWorkflow",
    "VideoPlanner",
    "RenderEngine",
]

__version__ = "0.4.0"
