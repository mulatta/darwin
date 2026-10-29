{
  projectRootFile = "flake.nix";
  programs = {
    # shell
    shellcheck.enable = true;
    shfmt.enable = true;

    # nix
    deadnix.enable = true;
    nixfmt.enable = true;

    # python
    ruff-check.enable = true;
    ruff-format.enable = true;
  };
}
