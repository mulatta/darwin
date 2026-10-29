{ pkgs, perSystem }:
pkgs.mkShellNoCC {
  packages = [
    pkgs.bash
    pkgs.cargo
    pkgs.coreutils
    pkgs.mypy
    pkgs.nodejs_22
    pkgs.pnpm_11
    perSystem.self.formatter
  ];
}
