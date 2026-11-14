"""Runtime policy names, compatibility interfaces and configuration errors."""

from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from optionkit.repository import load_policy


class RuntimePolicyCase(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(TOOLS / 'waypure-policy.toml')

    def test_x11_dependencies_cover_core_extensions_utilities_and_multilib(self):
        for name in (
            'libx11', 'libxcb', 'libxau', 'libxdmcp', 'libxaw', 'xaw3d',
            'libxfont2', 'libxft', 'libxkbfile', 'libxklavier', 'libxnvctrl',
            'libxp', 'libxpm', 'libxpresent', 'libxres', 'libxv',
            'libxvmc', 'libxxf86vm', 'libxxf86dga', 'libdmx', 'libfs',
            'libxprintutil', 'xcb-util-wm', 'xcb-imdkit', 'egl-x11',
            'lib32-libx11>=1.8', 'lib32-libxcb-waypure=1.17-1',
            'libx11-waypure', 'xorg-server-xvfb', 'xorg-xwayland',
            'motif', 'qt5-x11extras', 'python-xlib', 'xf86-video-amdgpu',
            'xdotool', 'libfakekey', 'libkeybinder3', 'libwnck3',
            'startup-notification', 'libvdpau',
            'libX11.so=6-64', 'lib:libX11.so.6', 'lib32:libxcb.so.1',
        ):
            with self.subTest(name=name):
                self.assertEqual(self.policy.dependency(name), 'x11')

    def test_x11_libraries_cover_bridge_and_vendor_names(self):
        for name in (
            'libX11-xcb.so.1', 'libXau.so.6', 'libXdmcp.so.6', 'libXmuu.so.1',
            'libXaw.so.7', 'libXaw6.so.6', 'libXaw7.so.7', 'libXaw3d.so.8',
            'libXfont.so.1', 'libXfont2.so.2', 'libXft.so.2', 'libxkbfile.so.1',
            'libxklavier.so.16', 'libXNVCtrl.so.0', 'libXp.so.6', 'libXpm.so.4',
            'libXpresent.so.1', 'libXRes.so.1',
            'libXv.so.1', 'libXvMC.so.1', 'libXvMCW.so.1', 'libXxf86vm.so.1',
            'libXxf86dga.so.1', 'libdmx.so.1', 'libFS.so.6', 'libXprintUtil.so.1',
            'libxcb-dri3.so.0', 'libxcb-imdkit.so.1', 'libxcb-ewmh.so.2',
            'libGLX_mesa.so.0', 'libGLX_nvidia.so.0', 'libnvidia-egl-xcb.so.1',
            'libnvidia-egl-xlib.so.1',
            'libXm.so.4', 'libQt6XcbQpa.so.6', 'libqxcb.so', 'libgdk-x11-2.0.so.0',
            'libxdo.so.3', 'libfakekey.so.0', 'libkeybinder-3.0.so.0',
            'libwnck-3.so.0', 'libstartup-notification-1.so.0',
            'libvdpau.so.1', 'libvdpau_trace.so.1',
        ):
            with self.subTest(name=name):
                self.assertEqual(self.policy.library(name), 'x11')

    def test_library_paths_match_only_the_posix_basename(self):
        for name, expected in (
            ('/usr/lib/libX11.so.6', 'x11'),
            ('../lib/libxcb.so.1', 'x11'),
            ('$ORIGIN/../lib/libjack.so.0', 'pipewire'),
            ('lib/libjackserver.so.0', 'pipewire'),
            ('/libX11.so.6/libEGL.so.1', None),
            ('$ORIGIN/libjack.so.0/notjack.so', None),
            ('/usr/lib/libx11.so.6', None),
            ('/usr/lib/prefixlibjack.so.0', None),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.policy.library(name), expected)

    def test_pipewire_rejects_jack_implementations_bridges_and_netjack(self):
        for name in (
            'jack', 'jack2', 'jack2-dbus', 'jack-example-tools', 'pipewire-jack',
            'pipewire-jack-client', 'pipewire-netjack2', 'pulseaudio-jack',
            'lib32-jack2', 'lib32-pipewire-jack', 'pipewire-netjack2-waypure',
            'jack>=0.126', 'libjack.so=0-64', 'lib:libjackserver.so.0',
            'lib32:libjacknet.so.0', 'pulseaudio', 'pulseaudio-bluetooth',
        ):
            with self.subTest(name=name):
                self.assertEqual(self.policy.dependency(name), 'pipewire')

    def test_pipewire_rejects_provided_jack_modules_without_libjack_linkage(self):
        for name in (
            'libjack.so.0', 'libjackserver.so.0', 'libjacknet.so.0',
            'libspa-jack.so', 'libasound_module_pcm_jack.so',
            'libpipewire-module-jack-tunnel.so',
            'libpipewire-module-jackdbus-detect.so',
            'libpipewire-module-netjack2-driver.so',
            'libpipewire-module-netjack2-manager.so', 'libpulsecore-17.0.so',
        ):
            with self.subTest(name=name):
                self.assertEqual(self.policy.library(name), 'pipewire')

    def test_wayland_and_pipewire_compatible_interfaces_remain_allowed(self):
        packages = (
            'libxkbcommon', 'lib32-libxkbcommon', 'libxml2', 'libxcrypt',
            'libxcvt', 'libfontenc', 'libxshmfence', 'xorgproto', 'wayland', 'mesa', 'libglvnd', 'glu',
            'libpulse', 'libpulse-waypure', 'pulseaudio-qt', 'pulseaudio-alsa', 'pulse-native-provider',
            'alsa-lib', 'alsa-utils', 'libpipewire', 'pipewire', 'pipewire-audio',
            'pipewire-pulse', 'pipewire-alsa', 'wireplumber', 'jackson-core',
        )
        libraries = (
            'libxkbcommon.so.0', 'libxml2.so.16', 'libcrypt.so.2', 'libxcvt.so.0',
            'libfontenc.so.1', 'libxshmfence.so.1',
            'libEGL.so.1', 'libOpenGL.so.0', 'libGLdispatch.so.0', 'libGLESv2.so.2',
            'libpulse.so.0', 'libpulse-simple.so.0', 'libpulsecommon-17.0.so',
            'libpulsedsp.so', 'libasound.so.2', 'libpipewire-0.3.so.0',
            'libasound_module_pcm_pipewire.so', 'libasound_module_ctl_pipewire.so',
        )
        for name in packages:
            with self.subTest(package=name):
                self.assertIsNone(self.policy.dependency(name))
        for name in libraries:
            with self.subTest(library=name):
                self.assertIsNone(self.policy.library(name))

    def test_alsa_based_interfaces_are_not_proof_of_a_competing_server(self):
        # sndio's Linux backend uses ALSA; JACK remains an explicit exception.
        self.assertIsNone(self.policy.dependency('sndio'))
        self.assertIsNone(self.policy.dependency('lib32-sndio'))
        self.assertIsNone(self.policy.library('libsndio.so.7'))

    def test_custom_x11_only_policy_does_not_inherit_audio_bans(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.toml'
            path.write_text('[dependencies]\nforbidden=["libx11"]\n'
                            '[libraries]\nforbidden=["libX11.so*"]\n')
            policy = load_policy(path)
            self.assertEqual(policy.dependency('libx11>=1'), 'x11')
            self.assertIsNone(policy.dependency('jack'))
            self.assertIsNone(policy.library('libjack.so.0'))

    def test_invalid_optional_audio_rules_fail_with_a_configuration_error(self):
        base = '[dependencies]\nforbidden=[]\n[libraries]\nforbidden=[]\n'
        invalid = (
            'pipewire = "invalid"\n' + base,
            base + '[pipewire]\nlibraries="invalid"\n',
            base + '[pipewire.dependencies]\nforbidden="jack"\n',
            base + '[pipewire.dependencies]\nforbidden=[3]\n',
            base + '[pipewire.dependencies]\nforbidden=["jack"]\n'
                   '[pipewire.libraries]\nforbidden=[""]\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.toml'
            for contents in invalid:
                with self.subTest(contents=contents):
                    path.write_text(contents)
                    with self.assertRaisesRegex(ValueError, 'pipewire'):
                        load_policy(path)

    def test_invalid_optional_shipped_path_rules_are_rejected(self):
        base = '[dependencies]\nforbidden=[]\n[libraries]\nforbidden=[]\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.toml'
            for contents in ('paths = []\n' + base, base + '[paths]\nforbidden="usr/bin/Xwayland"\n'):
                path.write_text(contents)
                with self.assertRaisesRegex(ValueError, 'paths'):
                    load_policy(path)
