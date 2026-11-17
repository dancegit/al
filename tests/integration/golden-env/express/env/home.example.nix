# Standalone Home Manager example using the generated module.
{ pkgs, ... }: {
  imports = [ ./home-manager.nix ];
  home.stateVersion = "25.05";
  programs."agentic-layer".enable = true;
}
