package com.firer.console.flexfactor;

/** Fixed, non-user-controlled scripts sent to Termux's documented RUN_COMMAND service. */
final class EngineRecoveryScript {
    static final String TERMUX_PACKAGE = "com.termux";
    static final String TERMUX_PERMISSION = "com.termux.permission.RUN_COMMAND";
    static final String TERMUX_SERVICE = "com.termux.app.RunCommandService";
    static final String TERMUX_ACTION = "com.termux.RUN_COMMAND";
    static final String BASH = "/data/data/com.termux/files/usr/bin/bash";
    static final String HOME = "/data/data/com.termux/files/home";

    static final String ENABLE_EXTERNAL_APPS_COMMAND = String.join("\n",
            "mkdir -p \"$HOME/.termux\"",
            "p=\"$HOME/.termux/termux.properties\"",
            "touch \"$p\"",
            "if grep -q '^[[:space:]]*allow-external-apps[[:space:]]*=' \"$p\"; then",
            "  sed -i 's/^[[:space:]]*allow-external-apps[[:space:]]*=.*/allow-external-apps=true/' \"$p\"",
            "else",
            "  printf '\\nallow-external-apps=true\\n' >> \"$p\"",
            "fi",
            "termux-reload-settings");

    private static final String PRELUDE = String.join("\n",
            "set -eu",
            "umask 077",
            "export HOME=/data/data/com.termux/files/home",
            "export PREFIX=/data/data/com.termux/files/usr",
            "export PATH=\"$HOME/.local/bin:$PREFIX/bin:/system/bin\"",
            "run_dir=\"$HOME/.phone-console\"",
            "mkdir -p \"$run_dir\"",
            "log=\"$run_dir/app-recovery.log\"",
            "exec >>\"$log\" 2>&1",
            "notify() {",
            "  /system/bin/am broadcast -W -n com.firer.console.flexfactor/.ConfigReceiver \\",
            "    --es recovery_status \"$1\" >/dev/null 2>&1 || true",
            "}");

    private EngineRecoveryScript() {}

    static String startScript() {
        return String.join("\n",
                PRELUDE,
                "app=\"$HOME/phone-console/flexfactor\"",
                "if [ ! -f \"$app/scripts/phone/engine.sh\" ]; then",
                "  notify missing-engine",
                "  exit 21",
                "fi",
                "notify starting",
                "if bash \"$app/scripts/phone/engine.sh\" start; then",
                "  notify ready",
                "else",
                "  notify failed",
                "  exit 22",
                "fi");
    }

    static String repairScript() {
        return String.join("\n",
                PRELUDE,
                "app=\"$HOME/phone-console/flexfactor\"",
                "notify updating",
                "if ! command -v git >/dev/null || ! command -v gh >/dev/null || ! command -v python >/dev/null; then",
                "  pkg update -y",
                "  pkg install -y python git gh nodejs-lts openssh which termux-api",
                "fi",
                "if ! gh auth status >/dev/null 2>&1; then",
                "  notify github-auth-required",
                "  exit 23",
                "fi",
                "gh auth setup-git",
                "mkdir -p \"$HOME/phone-console\"",
                "if [ -d \"$app/.git\" ]; then",
                "  if [ -n \"$(git -C \"$app\" status --porcelain)\" ]; then",
                "    notify checkout-dirty",
                "    exit 24",
                "  fi",
                "  git -C \"$app\" fetch --prune origin",
                "  git -C \"$app\" switch main",
                "  git -C \"$app\" pull --ff-only origin main",
                "else",
                "  gh repo clone buckeye7066/flexfactor \"$app\"",
                "fi",
                "FLEXFACTOR_SKIP_PACKAGES=1 FLEXFACTOR_NONINTERACTIVE=1 \\",
                "  bash \"$app/scripts/phone/setup.sh\"",
                "if bash \"$app/scripts/phone/engine.sh\" restart; then",
                "  notify ready",
                "else",
                "  notify failed",
                "  exit 25",
                "fi");
    }
}
