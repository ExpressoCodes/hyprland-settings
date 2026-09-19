{
  description = "Hyprland Settings — GUI monitor manager for Hyprland";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];

      forAllSystems = fn:
        nixpkgs.lib.genAttrs supportedSystems
          (system: fn nixpkgs.legacyPackages.${system});

      mkPackage = pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = "hyprland-settings";
          version = "0.1.0";
          pyproject = true;
          src = self;

          build-system = with pkgs.python3Packages; [ hatchling ];

          nativeBuildInputs = with pkgs; [
            gobject-introspection
            wrapGAppsHook4
            imagemagick
          ];

          buildInputs = with pkgs; [
            gtk4
            libadwaita
            glib
          ];

          dependencies = with pkgs.python3Packages; [ pygobject3 ];

          postInstall = ''
            install -Dm644 data/hyprland-settings.desktop \
              $out/share/applications/hyprland-settings.desktop
            substituteInPlace $out/share/applications/hyprland-settings.desktop \
              --replace "Exec=hyprland-settings" "Exec=$out/bin/hyprland-settings"
            install -Dm644 data/hyprland-settings.png \
              $out/share/icons/hicolor/256x256/apps/hyprland-settings.png
            mkdir -p $out/share/icons/hicolor/128x128/apps
            magick data/hyprland-settings.png -resize 128x128 \
              $out/share/icons/hicolor/128x128/apps/hyprland-settings.png
            mkdir -p $out/share/icons/hicolor/64x64/apps
            magick data/hyprland-settings.png -resize 64x64 \
              $out/share/icons/hicolor/64x64/apps/hyprland-settings.png
            mkdir -p $out/share/icons/hicolor/48x48/apps
            magick data/hyprland-settings.png -resize 48x48 \
              $out/share/icons/hicolor/48x48/apps/hyprland-settings.png
          '';

          meta = with pkgs.lib; {
            description = "GUI monitor manager for Hyprland compositor";
            homepage = "https://github.com/ExpressoCodes/hyprland-settings";
            license = licenses.mit;
            mainProgram = "hyprland-settings";
            platforms = platforms.linux;
          };
        };
    in
    {
      # Overlay — adds pkgs.hyprland-settings to your nixpkgs
      overlays.default = final: prev: {
        hyprland-settings = mkPackage final;
      };

      # Standalone packages (nix build / nix run)
      packages = forAllSystems (pkgs: {
        default = mkPackage pkgs;
        hyprland-settings = mkPackage pkgs;
      });

      # Development shell — `nix develop`
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          inputsFrom = [ (mkPackage pkgs) ];
          packages = with pkgs; [
            python3Packages.pytest
            python3Packages.ruff
          ];
        };
      });
    };
}
