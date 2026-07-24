# Homebrew formula skeleton for the rrotor runtime CLI — `brew install rrotor`.
#
# This installs the rrotor runtime as a local CLI (docs/hosting.md §2): the same
# binary that runs in the cloud, pointed at a local SQLite stator under ~/.rrotor.
#
# Status: SKELETON. `url`/`sha256` point at a release tarball that the release
# pipeline must publish (a packaged `dist` + prod deps, or — preferred follow-up — a
# self-contained Node SEA / bun binary so this formula needs no `node` dependency).
# Until a standalone binary exists, the formula depends on `node`.
class Rrotor < Formula
  desc "rrotor — the deterministic AI agent-loop runtime (RotorSpec)"
  homepage "https://github.com/glyphh-ai/rrotor"
  # Replace with the published release artifact + checksum:
  url "https://github.com/glyphh-ai/rrotor/releases/download/v0.0.1/rrotor-0.0.1.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "Apache-2.0"
  version "0.0.1"

  depends_on "node" # drop once a self-contained binary is shipped (SEA/bun)

  def install
    # The release tarball contains the packaged CLI (dist + node_modules).
    libexec.install Dir["*"]
    # Wrap the entrypoint so `rrotor …` runs the bundled runtime under Homebrew's node.
    (bin/"rrotor").write_env_script libexec/"dist/cli.js", NODE_ENV: "production"
  end

  test do
    assert_match "rrotor v", shell_output("#{bin}/rrotor version")
  end
end
