"""Descriptive stack categories, separate from the repository's rejection policy."""
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path,PurePosixPath
from .runtime_policy import load_policy

FAMILIES={
    'jack':('audio','JACK APIs, servers and compatibility'),
    'alsa':('audio','ALSA clients, device tools and plugins'),
    'pulse':('audio','PulseAudio protocol clients and servers'),
    'pipewire':('audio','PipeWire clients, servers, SPA and compatibility'),
    'oss':('audio','OSS compatibility'), 'sndio':('audio','sndio'),
    'portaudio':('audio','PortAudio abstraction'), 'openal':('audio','OpenAL abstraction'),
    'legacy-gl':('graphics','libGL compatibility API (not proof of GLX)'),
    'opengl':('graphics','OpenGL API and driver support'), 'glvnd':('graphics','GLVND dispatch'),
    'egl':('graphics','EGL'), 'gles':('graphics','OpenGL ES'),
    'glx':('graphics','GLX'), 'vulkan':('graphics','Vulkan loaders, ICDs and layers'),
    'osmesa':('graphics','Off-screen Mesa'), 'gbm':('graphics','Generic Buffer Management'),
    'drm':('graphics','Direct Rendering Manager userspace'),
    'xlib':('x11','Xlib and X extension libraries'), 'xcb':('x11','XCB'),
    'xorg':('x11','Xorg tools, server and development files'),
    'xwayland':('x11','XWayland'), 'x11-glx':('x11','GLX display integration'),
    'x11-integration':('x11','X11 toolkit, window-manager and display integration'),
}
# (family, role, package patterns). Replacement and lib32 prefixes normalize below.
PACKAGE_RULES=(
 ('jack','server',('jack','jack2','jack2-*')), ('jack','compatibility',('pipewire-jack','pipewire-jack-*','pipewire-netjack*')),
 ('jack','component',('jack-*','libjack','libjack-*','qjackctl')),
 ('alsa','client',('alsa-lib','tinyalsa')), ('alsa','plugins',('alsa-plugins',)),
 ('alsa','configuration',('alsa-ucm-conf','alsa-topology-conf','alsa-card-profiles')), ('alsa','tools',('alsa-utils','alsa-tools','alsa-firmware')),
 ('alsa','compatibility',('pipewire-alsa','pulseaudio-alsa','alsa-oss')),
 ('pulse','server',('pulseaudio','pulseaudio-bluetooth','pulseaudio-jack','pulseaudio-rtp','pulseaudio-zeroconf','pulseaudio-equalizer','pulseaudio-lirc')),
 ('pulse','client',('libpulse','pulseaudio-qt','libcanberra-pulse')),
 ('pulse','compatibility',('pipewire-pulse','pulseaudio-alsa','pulse-native-provider')),
 ('pipewire','server',('pipewire',)), ('pipewire','client',('libpipewire',)),
 ('pipewire','session-manager',('wireplumber','pipewire-media-session')),
 ('pipewire','compatibility',('pipewire-alsa','pipewire-pulse','pipewire-jack','pipewire-jack-*','pipewire-netjack*')),
 ('pipewire','component',('pipewire-audio','pipewire-v4l2','pipewire-roc','gst-plugin-pipewire')),
 ('oss','compatibility',('alsa-oss','osspd')), ('sndio','component',('sndio',)),
 ('portaudio','client',('portaudio','portaudiocpp')), ('openal','client',('openal',)),
 ('legacy-gl','client',('libgl',)), ('opengl','driver',('mesa','nvidia-utils','nvidia-*-utils')),
 ('glvnd','dispatcher',('libglvnd',)), ('egl','integration',('egl-wayland','egl-gbm','egl-x11')),
 ('vulkan','loader',('vulkan-icd-loader',)),
 ('vulkan','driver',('vulkan-radeon','vulkan-intel','vulkan-nouveau','vulkan-swrast','amdvlk','vulkan-dzn','vulkan-virtio')),
 ('vulkan','development',('vulkan-headers','vulkan-validation-layers','vulkan-utility-libraries','vulkan-extra-layers')),
 ('vulkan','tools',('vulkan-tools',)), ('osmesa','client',('osmesa',)),
 ('gbm','client',('libgbm',)), ('drm','client',('libdrm',)),
 ('xwayland','server',('xorg-xwayland','xwayland')), ('xcb','client',('libxcb','xcb-util*')),
 ('xorg','component',('xorg-*','xf86-*','xbitmaps','xkeyboard-config','xtrans','xproto','xcb-proto')),
)
LIBRARY_RULES=(
 ('jack',('libjack.so*','libjacknet.so*','libjackserver.so*','libspa-jack*.so*','libasound_module_pcm_jack.so*','libpipewire-module-*jack*.so*')),
 ('alsa',('libasound.so*','libatopology.so*','libtinyalsa.so*','libasound_module_*.so*')),
 ('pulse',('libpulse.so*','libpulsecommon-*.so*','libpulse-simple.so*','libpulse-mainloop-glib.so*','libpulsecore-*.so*','libasound_module_*_pulse.so*','libpipewire-module-protocol-pulse.so*')),
 ('pipewire',('libpipewire-*.so*','libspa-*.so*','libwireplumber-*.so*','libasound_module_*_pipewire.so*')),
 ('oss',('liboss*.so*','libaoss.so*')), ('sndio',('libsndio.so*',)),
 ('portaudio',('libportaudio.so*','libportaudiocpp.so*')), ('openal',('libopenal.so*',)),
 ('legacy-gl',('libGL.so*',)), ('opengl',('libOpenGL.so*','libglapi.so*','libnvidia-glcore.so*','libnvidia-glsi.so*')), ('glvnd',('libGLdispatch.so*',)),
 ('egl',('libEGL.so*','libEGL_*.so*','libnvidia-egl-*.so*')),
 ('gles',('libGLESv1_CM.so*','libGLESv2.so*','libGLESv3.so*')),
 ('glx',('libGLX.so*','libGLX_*.so*','libglx.so*')), ('x11-glx',('libGLX.so*','libGLX_*.so*','libglx.so*')),
 ('vulkan',('libvulkan.so*','libvulkan_*.so*','libVkLayer_*.so*','libnvidia-vulkan*.so*')),
 ('osmesa',('libOSMesa.so*',)), ('gbm',('libgbm.so*',)), ('drm',('libdrm.so*','libdrm_*.so*')),
 ('xcb',('libxcb.so*','libxcb-*.so*','libX11-xcb.so*')),
)

@lru_cache(maxsize=1)
def x_policy():
    return load_policy(Path(__file__).resolve().parents[1]/'waypure-policy.toml')

def match(family,role):
    return {'domain':FAMILIES[family][0],'family':family,'role':role}

@lru_cache(maxsize=8192)
def package_matches(name):
    name=name.removeprefix('lib32-').removesuffix('-waypure').removesuffix('-pipepure')
    found=[match(family,role) for family,role,patterns in PACKAGE_RULES if any(fnmatchcase(name,p) for p in patterns)]
    if x_policy().dependency(name)=='x11' and not any(v['domain']=='x11' for v in found):
        family='xlib' if name in {'libx11','libxext','libxrender','libxrandr','libxi','libxfixes','libxcursor','libxcomposite','libxdamage','libxinerama','libxt','libxau','libxdmcp','libxss','libxv','libxxf86vm','libice','libsm'} else 'x11-integration'
        found.append(match(family,'component'))
    return tuple(found)

@lru_cache(maxsize=8192)
def library_matches(library):
    name=PurePosixPath(library).name
    found=[match(family,'library') for family,patterns in LIBRARY_RULES if any(fnmatchcase(name,p) for p in patterns)]
    if x_policy().library(name)=='x11' and not any(v['domain']=='x11' for v in found):
        family='xlib' if name.startswith(('libX','libICE.','libSM.')) else 'x11-integration'
        found.append(match(family,'library'))
    return tuple(found)

def is_debug_path(path):
    value=path.lstrip('/')
    return value.startswith(('usr/lib/debug/','usr/lib32/debug/','usr/src/debug/','lib/debug/')) or value.endswith('.debug')

def path_matches(path):
    if path.endswith('/') or is_debug_path(path):return []
    found=list(library_matches(path))
    if path.endswith('.json') and '/vulkan/icd.d/' in '/'+path:found.append(match('vulkan','ICD-manifest'))
    elif path.endswith('.json') and any(v in '/'+path for v in ('/vulkan/implicit_layer.d/','/vulkan/explicit_layer.d/')):found.append(match('vulkan','layer-manifest'))
    if path.endswith('/pw-jack'):found.append(match('jack','compatibility'))
    return found
