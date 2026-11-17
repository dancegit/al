# The feature bundles of `(toolchain nix #:features (…))` and what every sandbox image needs.
# Copied unchanged into a layer's env/ by `al gen`; env/toolchain-<id>.nix imports it.
#
#   pkgs, lib   the pinned nixpkgs (or, in the Home Manager module, the user's own)
#   agentCli    env/agent-cli.json: per agent kind, its nixpkgs attribute and the vendor fallback
#   agents      the agent kinds whose CLI `agent-cli` provides, e.g. [ "claude-code" ]
{ pkgs, lib, agentCli, agents }:
let
  system = pkgs.stdenv.hostPlatform.system;

  fontsConf = pkgs.makeFontsConf {
    fontDirectories = with pkgs; [ dejavu_fonts liberation_ttf noto-fonts ];
  };

  # In the image only: the container is the sandbox, and chromium's own sandbox cannot start with
  # every capability dropped.
  browserWrapper = pkgs.writeShellScriptBin "chromium" ''
    exec ${pkgs.chromium}/bin/chromium --no-sandbox --disable-dev-shm-usage "$@"
  '';

  browserEnv = chrome: {
    CHROME_BIN = chrome;
    CHROME_PATH = chrome;
    PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH = chrome;
    PUPPETEER_EXECUTABLE_PATH = chrome;
    PUPPETEER_SKIP_DOWNLOAD = "1";
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
    FONTCONFIG_FILE = "${fontsConf}";
  };

  # The vendor's release of an agent CLI, pinned by the SRI hash in agent-cli.json.
  vendorCli = kind: spec:
    let
      fallback = spec.fallback;
      source = fallback.systems.${system}
        or (throw "agent-cli: no ${kind} release for ${system} in agent-cli.json, and nixpkgs has no recent enough `${spec.nixpkgs}`");
      variables = lib.concatStringsSep " "
        (lib.mapAttrsToList (name: value: "--set ${name} ${lib.escapeShellArg value}") (spec.env or { }));
    in
    pkgs.stdenv.mkDerivation {
      pname = kind;   # the name allowUnfreePredicate sees
      version = fallback.version;
      src = pkgs.fetchurl { url = source.url; hash = source.hash; };
      nativeBuildInputs = [ pkgs.makeWrapper ]
        ++ lib.optional (fallback.patchelf && pkgs.stdenv.hostPlatform.isElf) pkgs.autoPatchelfHook;
      dontStrip = true;   # the binaries carry their payload; stripping would break them
      dontConfigure = true;
      dontBuild = true;
      sourceRoot = ".";
      unpackPhase = "tar xzf $src";
      installPhase = ''
        mkdir -p $out/libexec/${kind} $out/bin
        cp -r package/. $out/libexec/${kind}/
        makeWrapper $out/libexec/${kind}/${lib.removePrefix "package/" source.path} $out/bin/${spec.binary} ${variables}
      '';
      meta = {
        description = "${kind} CLI ${fallback.version} (vendor release)";
        license = if lib.elem kind agentCli.unfree then lib.licenses.unfree else lib.licenses.asl20;
        platforms = lib.attrNames fallback.systems;
      };
    };

  # nixpkgs' package when the pinned nixpkgs has it at no less than minVersion (or there is no
  # vendor release for this system); else the vendor release.
  agentCliPackage = kind:
    let
      spec = agentCli.${kind} or (throw "agent-cli: al knows no CLI for agent kind ${kind}");
      attr = spec.nixpkgs;
      inNixpkgs = pkgs ? ${attr};
      recent = inNixpkgs && lib.versionAtLeast (pkgs.${attr}.version or "0") spec.minVersion;
      vendorHere = spec.fallback.systems ? ${system};
    in
    if recent || (inNixpkgs && !vendorHere) then pkgs.${attr} else vendorCli kind spec;

  byFeature = {
    # A headless browser: chromium, fonts, and the variables that make Playwright and Puppeteer use
    # it instead of downloading their own.
    browser = {
      packages = with pkgs; [ chromium fontconfig dejavu_fonts liberation_ttf noto-fonts ];
      env = browserEnv "${pkgs.chromium}/bin/chromium";
    };
    # The CLI of every agent kind in `agents` (claude, codex).
    agent-cli = {
      packages = map agentCliPackage agents;
      env = { };
    };
    # ffmpeg, for recordings and keyframes.
    video = {
      packages = with pkgs; [ ffmpeg ];
      env = { };
    };
    # A C compiler and build tools.
    build-essentials = {
      packages = with pkgs; [ gcc gnumake pkg-config ];
      env = { };
    };
    # The docker client (the daemon is yours).
    docker-cli = {
      packages = with pkgs; [ docker-client ];
      env = { };
    };
    # The podman CLI.
    podman-cli = {
      packages = with pkgs; [ podman ];
      env = { };
    };
  };

  # The image swaps chromium for its --no-sandbox wrapper.
  imageFeature = name:
    if name == "browser" then {
      packages = [ browserWrapper ] ++ (with pkgs; [ fontconfig dejavu_fonts liberation_ttf noto-fonts ]);
      env = browserEnv "${browserWrapper}/bin/chromium";
    } else byFeature.${name};

  collect = get: names: lib.unique (lib.concatMap (name: (get name).packages) names);
  merge = get: names: lib.foldl' (acc: name: acc // (get name).env) { } names;
in
{
  inherit byFeature browserWrapper;

  packagesFor = collect (name: byFeature.${name});
  envFor = merge (name: byFeature.${name});

  image = {
    packagesFor = collect imageFeature;
    envFor = merge imageFeature;
  };

  # What the runtime itself needs inside any image: a shell and the usual tools for `bash -c CMD`,
  # CA certificates, and git (the run's commits are made inside the container).
  imageBase = with pkgs; [ bashInteractive coreutils findutils gnugrep gnused gnutar gzip which cacert git ];
}
