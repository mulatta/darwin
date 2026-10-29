{ pkgs, perSystem }:
pkgs.mkShellNoCC {
  packages = [
    # The agent's kernel env: runtime and bundled skills.
    perSystem.self.prime-agent.passthru.pythonRuntime
    # prime-agent with the darwin skills (artifact, pair, record) bundled.
    perSystem.self.prime-agent

    pkgs.bash
    pkgs.cargo
    pkgs.coreutils

    pkgs.mypy
    pkgs.nodejs_22
    pkgs.pnpm_11

    perSystem.self.formatter
  ];
  # pair starts marimo servers from this env; the wrapper sets it for the agent.
  DARWIN_MARIMO_PYTHON = "${perSystem.self.prime-agent.passthru.marimoServer}/bin/python3";
}
