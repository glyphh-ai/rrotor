from pathlib import Path

from rrotord.sandbox import bwrap
from rrotord.sandbox.bwrap import Limits, Mounts

MOUNTS = Mounts(Path("/d/src"), Path("/d/venv"), Path("/sdk/rrotor"), Path("/d/files"), None)


def after(argv: list[str], flag: str) -> list[list[str]]:
    return [argv[i + 1 : i + 3] for i, a in enumerate(argv) if a == flag]


def test_landlock_rules_without_proxy_or_data():
    rules = bwrap.landlock_rules(MOUNTS)
    assert rules["write"] == ["/files", "/tmp", "/dev"] and rules["tcp_ports"] == []
    assert "/program" in rules["read"]  # type: ignore[operator]


def test_landlock_rules_with_proxy_and_data():
    mounts = Mounts(Path("/s"), Path("/v"), Path("/k"), Path("/f"), Path("/p.sock"), Path("/data/x"))
    rules = bwrap.landlock_rules(mounts)
    assert rules["write"] == ["/files", "/tmp", "/dev", "/data"] and rules["tcp_ports"] == [3128]


def test_prlimit_holds_memory_cpu_files_and_no_core():
    assert bwrap.prlimit(Limits(512, 65)) == [
        "prlimit",
        f"--data={512 * 1024 * 1024}",
        "--cpu=65",
        "--nofile=256",
        f"--fsize={1024 * 1024 * 1024}",
        "--core=0",
        "--",
    ]


def test_command_without_proxy_binds_only_what_the_run_has():
    argv = bwrap.command(MOUNTS, Limits(256, 10), 7, 9, exists=lambda p: p == Path("/usr"))
    assert argv[argv.index("bwrap") + 1 : argv.index("bwrap") + 5] == [
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ]
    assert after(argv, "--ro-bind") == [
        ["/usr", "/usr"],
        ["/d/src", "/program"],
        ["/d/venv", "/venv"],
        ["/sdk/rrotor", "/opt/rrotor/rrotor"],
    ]
    assert after(argv, "--bind") == [["/d/files", "/files"]]
    assert after(argv, "--symlink") == []
    env = dict(after(argv, "--setenv"))
    assert env["RROTOR_CHANNEL_FD"] == "9" and "HTTPS_PROXY" not in env
    assert argv[-8:] == ["--seccomp", "7", "--chdir", "/program", "--", "/venv/bin/python", "-m", "rrotor._worker"]


def test_command_with_proxy_and_data():
    mounts = Mounts(Path("/s"), Path("/v"), Path("/k"), Path("/f"), Path("/p.sock"), Path("/d"))
    argv = bwrap.command(mounts, Limits(256, 10), 7, 9, exists=lambda p: True)
    assert ["/d", "/data"] in after(argv, "--bind") and ["/p.sock", "/run/rrotor/proxy.sock"] in after(argv, "--bind")
    assert ["usr/bin", "/bin"] in after(argv, "--symlink")
    assert dict(after(argv, "--setenv"))["HTTPS_PROXY"] == "http://127.0.0.1:3128"


def test_command_checks_the_real_filesystem_by_default():
    argv = bwrap.command(MOUNTS, Limits(256, 10), 7, 9)
    assert ["/usr", "/usr"] in after(argv, "--ro-bind")


def test_a_followers_run_has_its_folder_and_data_read_only():
    mounts = Mounts(Path("/s"), Path("/v"), Path("/k"), Path("/f"), None, Path("/data/x"), readonly=True)
    rules = bwrap.landlock_rules(mounts)
    assert rules["write"] == ["/tmp", "/dev"] and {"/files", "/data"} <= set(rules["read"])  # type: ignore[call-overload]
    argv = bwrap.command(mounts, Limits(256, 10), 7, 9, exists=lambda p: False)
    assert ["/f", "/files"] in after(argv, "--ro-bind") and ["/data/x", "/data"] in after(argv, "--ro-bind")
    assert after(argv, "--bind") == []


def test_a_clis_run_mounts_its_install_working_folder_and_home():
    mounts = Mounts(MOUNTS.src, MOUNTS.venv, MOUNTS.sdk, MOUNTS.files, None, cli=Path("/i"), work=Path("/w"))
    mounts_home = Mounts(MOUNTS.src, MOUNTS.venv, MOUNTS.sdk, MOUNTS.files, None, cli=Path("/i"), home=Path("/h"))
    rules = bwrap.landlock_rules(mounts)
    assert "/cli" in rules["read"] and rules["write"] == ["/files", "/tmp", "/dev", "/work"]  # type: ignore[operator]
    assert rules["exec_writes"] is False and bwrap.landlock_rules(MOUNTS)["exec_writes"] is True
    assert "/home/cli" in bwrap.landlock_rules(mounts_home)["write"]  # type: ignore[operator]
    argv = bwrap.command(mounts, Limits(256, 10), 7, 9, exists=lambda p: False)
    assert ["/i", "/cli"] in after(argv, "--ro-bind") and ["/w", "/work"] in after(argv, "--bind")
    home = bwrap.command(mounts_home, Limits(256, 10), 7, 9, exists=lambda p: False)
    assert ["/h", "/home/cli"] in after(home, "--bind")


def test_a_sign_in_that_listens_is_not_held_to_the_proxys_port():
    mounts = Mounts(MOUNTS.src, MOUNTS.venv, MOUNTS.sdk, MOUNTS.files, Path("/p"), cli=Path("/i"), listen=True)
    assert bwrap.landlock_rules(mounts)["net"] is False and bwrap.landlock_rules(MOUNTS)["net"] is True
    follower = Mounts(MOUNTS.src, MOUNTS.venv, MOUNTS.sdk, MOUNTS.files, None, home=Path("/h"), readonly=True)
    on_follower = bwrap.command(follower, Limits(256, 10), 7, 9, exists=lambda p: False)
    assert ["/h", "/home/cli"] in after(on_follower, "--ro-bind")
    assert "/home/cli" in bwrap.landlock_rules(follower)["read"]  # type: ignore[operator]


def test_the_folders_a_run_is_given_are_bound_at_their_own_paths_as_chosen():
    given = (("/home/ana/docs/out", True), ("/home/ana/docs", False), ("/srv/shared", True))
    mounts = Mounts(MOUNTS.src, MOUNTS.venv, MOUNTS.sdk, MOUNTS.files, None, folders=given)
    argv = bwrap.command(mounts, Limits(256, 10), 7, 9, exists=lambda p: False)
    assert ["/home/ana/docs", "/home/ana/docs"] in after(argv, "--ro-bind")
    assert after(argv, "--bind")[1:] == [["/home/ana/docs/out", "/home/ana/docs/out"], ["/srv/shared", "/srv/shared"]]
    # a folder is bound before the folders inside it, so the inner one's say holds
    assert argv.index("/home/ana/docs") < argv.index("/home/ana/docs/out")
    rules = bwrap.landlock_rules(mounts)
    assert rules["write"] == ["/files", "/tmp", "/dev", "/home/ana/docs/out", "/srv/shared"]
    assert rules["read"][-1] == "/home/ana/docs"  # type: ignore[index]
    assert "/home/ana" not in rules["read"] and "/home" not in rules["read"]  # type: ignore[operator]
    # with none given, nothing of the machine is bound
    plain = bwrap.command(MOUNTS, Limits(256, 10), 7, 9, exists=lambda p: False)
    assert after(plain, "--bind") == [["/d/files", "/files"]]
    # on a cluster's follower nothing is written, a folder given to change included
    follower = Mounts(MOUNTS.src, MOUNTS.venv, MOUNTS.sdk, MOUNTS.files, None, readonly=True, folders=given)
    on_follower = bwrap.command(follower, Limits(256, 10), 7, 9, exists=lambda p: False)
    assert after(on_follower, "--bind") == [] and ["/srv/shared", "/srv/shared"] in after(on_follower, "--ro-bind")
    assert bwrap.landlock_rules(follower)["write"] == ["/tmp", "/dev"]
    assert "/srv/shared" in bwrap.landlock_rules(follower)["read"]  # type: ignore[operator]
