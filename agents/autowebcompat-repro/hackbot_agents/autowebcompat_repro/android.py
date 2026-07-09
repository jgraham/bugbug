import logging
import os
import platform
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import requests
from mozrunner.devices import android_device

logger = logging.getLogger("autowebcompat-repro")

here = os.path.abspath(os.path.dirname(__file__))

CMDLINE_TOOLS_VERSION_STRING = "12.0"
CMDLINE_TOOLS_VERSION = "11076708"

AVD_MANIFEST_X86_64 = {
    "emulator_package": "system-images;android-34;google_apis;x86_64",
    "emulator_avd_name": "mozemulator-android34-x86_64",
}


@dataclass
class AndroidPaths:
    base: Path
    sdk: Path
    sdk_tools: Path
    avd: Path
    emulator_home: Path


def configure_device(paths: AndroidPaths) -> None:
    android_device.EMULATOR_HOME_DIR = str(paths.emulator_home)
    android_device.AVD_DICT["x86_64"] = android_device.AvdInfo(
        "Android x86_64",
        "mozemulator-android34-x86_64",
        [
            "-skip-adb-auth",
            "-verbose",
            "-show-kernel",
            "-ranchu",
            "-selinux",
            "permissive",
            "-memory",
            "4096",
            "-cores",
            "4",
            "-prop",
            "ro.test_harness=true",
            "-no-snapstorage",
            "-no-snapshot",
            "-no-metrics",
            "-skin",
            "800x1280",
        ],
        True,
    )


def get_paths(base_path: Path) -> AndroidPaths:
    os_name = platform.system().lower()

    sdk_path = Path(
        os.environ.get("ANDROID_SDK_HOME", base_path / f"android-sdk-{os_name}")
    )
    avd_path = Path(os.environ.get("ANDROID_AVD_HOME", sdk_path / ".android" / "avd"))
    return AndroidPaths(
        base=base_path,
        sdk=sdk_path,
        sdk_tools=sdk_path / "cmdline-tools" / CMDLINE_TOOLS_VERSION_STRING,
        avd=avd_path,
        emulator_home=avd_path.parent,
    )


def get_sdk_manager_path(paths: AndroidPaths) -> Path:
    os_name = platform.system().lower()
    file_name = "sdkmanager"
    if os_name.startswith("win"):
        file_name += ".bat"
    return paths.sdk_tools / "bin" / file_name


def get_avd_manager(paths: AndroidPaths) -> Path:
    os_name = platform.system().lower()
    file_name = "avdmanager"
    if os_name.startswith("win"):
        file_name += ".bat"
    return paths.sdk_tools / "bin" / file_name


def uninstall_sdk(paths: AndroidPaths) -> None:
    if paths.sdk.exists() and paths.sdk.is_dir():
        shutil.rmtree(paths.sdk)


def get_os_tag() -> str:
    os_name = platform.system().lower()
    if os_name not in ["darwin", "linux", "windows"]:
        logger.critical("Unsupported platform %s" % os_name)
        raise NotImplementedError

    if os_name == "macosx":
        return "darwin"
    if os_name == "windows":
        return "win"
    return "linux"


def download_and_extract(url: str, path: Path) -> None:
    if not path.exists():
        os.makedirs(path)
    temp_path = path / url.rsplit("/", 1)[1]
    try:
        with open(temp_path, "wb") as f:
            with requests.get(url, stream=True) as resp:
                for chunk in resp.iter_content(2**16):
                    f.write(chunk)
        if not temp_path.exists():
            raise ValueError(f"Failed to download {url}, output path doesn't exist")
        # Python's zipfile module doesn't seem to work here
        subprocess.check_call(["unzip", temp_path], cwd=path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def install_sdk(paths: AndroidPaths) -> bool:
    if os.path.isdir(paths.sdk_tools):
        logger.info("Using SDK installed at %s", paths.sdk_tools)
        return False

    if not os.path.exists(paths.sdk):
        os.makedirs(paths.sdk)

    download_path = paths.sdk_tools.parent

    url = f"https://dl.google.com/android/repository/commandlinetools-{get_os_tag()}-{CMDLINE_TOOLS_VERSION}_latest.zip"
    logger.info("Getting SDK from %s" % url)

    download_and_extract(url, download_path)
    os.rename(os.path.join(download_path, "cmdline-tools"), paths.sdk_tools)

    return True


def install_android_packages(
    paths: AndroidPaths, packages: list[str], prompt: bool = True
) -> None:
    sdk_manager = get_sdk_manager_path(paths)
    if not sdk_manager.exists():
        raise OSError(f"Can't find sdkmanager at {sdk_manager}")

    # TODO: make this work non-internactively
    logger.info(f"Installing Android packages {' '.join(packages)}")
    cmd = [str(sdk_manager)] + packages

    input_data = None if prompt else b"y\n"
    subprocess.run(cmd, check=True, input=input_data)


def install_avd(paths: AndroidPaths, prompt: bool = True):
    avd_manager = get_avd_manager(paths)
    avd_manifest = AVD_MANIFEST_X86_64

    install_android_packages(paths, [avd_manifest["emulator_package"]], prompt=prompt)

    cmd = [
        str(avd_manager),
        "--verbose",
        "create",
        "avd",
        "--force",
        "--name",
        avd_manifest["emulator_avd_name"],
        "--package",
        avd_manifest["emulator_package"],
    ]
    input_data = None if prompt else b"no"
    subprocess.run(cmd, check=True, input=input_data)


def get_emulator(
    paths: AndroidPaths, device_serial: str | None = None
) -> android_device.AndroidEmulator:
    substs = {
        "top_srcdir": str(Path(__file__).parent.parent.parent.parent.absolute()),
        "TARGET_CPU": platform.uname().machine,
        "HOST_CPU_ARCH": platform.uname().machine,
    }
    emulator = android_device.AndroidEmulator(
        substs=substs, device_serial=device_serial, verbose=True
    )
    emulator.emulator_path = str(paths.sdk / "emulator" / "emulator")
    return emulator


class Environ:
    def __init__(self, **kwargs):
        self.environ = None
        self.set_environ = kwargs

    def __enter__(self) -> None:
        self.environ = os.environ.copy()
        for key, value in self.set_environ.items():
            if value is None:
                if key in os.environ:
                    del os.environ[key]
            else:
                os.environ[key] = value

    def __exit__(self, *args, **kwargs) -> None:
        assert self.environ is not None
        # Copy the old environ back over
        for key in os.environ.keys():
            if key not in self.environ:
                del os.environ[key]
        for key, value in self.environ.items():
            os.environ[key] = value


def android_environment(paths: AndroidPaths):
    return Environ(
        ANDROID_EMULATOR_HOME=str(paths.emulator_home),
        ANDROID_AVD_HOME=str(paths.avd),
        ANDROID_SDK_ROOT=str(paths.sdk),
        ANDROID_SDK_HOME=str(paths.sdk),
    )


def install(dest: Path, reinstall: bool = False, prompt: bool = True):
    paths = get_paths(dest)
    configure_device(paths)

    with android_environment(paths):
        if reinstall:
            uninstall_sdk(paths)

        new_install = install_sdk(paths)

        if new_install:
            packages = [
                "platform-tools",
                "build-tools;37.0.0",
                "platforms;android-37.0",
                "emulator",
            ]

            install_android_packages(paths, packages, prompt=prompt)

            install_avd(paths, prompt=prompt)

        emulator = get_emulator(paths)
    return emulator


def cancel_start(thread_id):
    def cancel_func():
        signal.pthread_kill(thread_id, signal.SIGINT)

    return cancel_func


def start(dest: Path, reinstall: bool = False, prompt=True, device_serial=None):
    paths = get_paths(dest)
    configure_device(paths)

    with android_environment(paths):
        install(dest=dest, reinstall=reinstall, prompt=prompt)

        emulator = get_emulator(paths, device_serial=device_serial)

        if not emulator.check_avd():
            raise Exception("AVD not installed")

        emulator.start()
        timer = threading.Timer(300, cancel_start(threading.get_ident()))
        timer.start()
        for i in range(10):
            logger.info(f"Wait for emulator to start attempt {i + 1}/10")
            try:
                emulator.wait_for_start()
            except Exception:
                import traceback

                logger.warning(f"""emulator.wait_for_start() failed:
{traceback.format_exc()}""")
            else:
                break
        timer.cancel()
    return emulator
