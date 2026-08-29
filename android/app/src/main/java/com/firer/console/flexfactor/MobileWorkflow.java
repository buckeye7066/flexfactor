package com.firer.console.flexfactor;

/** The small caller installed into each selected repository by the Android app. */
final class MobileWorkflow {
    static final String PATH = ".github/workflows/flexfactor-mobile.yml";
    static final String FILE_NAME = "flexfactor-mobile.yml";

    // The release tag android-client.yml publishes is exactly
    // "android-v${versionName}", so the engine this build was tested against is
    // derivable from the build itself. It used to be a hand-typed literal, and
    // it drifted: the shipped 3.2.1 app carried the previous release's tag and
    // therefore installed a caller that ran that older engine, whose validator
    // read the caller's event payload instead of the reusable workflow's
    // inputs. Every phone run died on "invalid repository" at its first step.
    // A version string typed in more than one place is a version string that
    // will disagree with itself; there is now only one. (The drift above was
    // 3.2.1 shipping with the 3.2.0 tag typed into this field.)
    static final String ENGINE_REF = "android-v" + BuildConfig.VERSION_NAME;

    private MobileWorkflow() {}

    static String content() {
        return String.join("\n",
                "name: FlexFactor Mobile",
                "",
                "run-name: \"FlexFactor ${{ inputs.mode }} · ${{ inputs.request_id }}\"",
                "",
                "on:",
                "  workflow_dispatch:",
                "    inputs:",
                "      request_id:",
                "        required: true",
                "        type: string",
                "      mode:",
                "        required: true",
                "        type: choice",
                "        options: [refactor, scout, audit, prodready]",
                "      provider:",
                "        required: true",
                "        type: choice",
                "        options: [ollama, openai, anthropic, copilot]",
                "      target_ref:",
                "        required: true",
                "        type: string",
                "      file:",
                "        required: false",
                "        type: string",
                "      goal:",
                "        required: false",
                "        type: string",
                "      scout_apply:",
                "        required: true",
                "        type: boolean",
                "      max_cost:",
                "        required: true",
                "        type: string",
                "      threshold:",
                "        required: true",
                "        type: string",
                "      max_iterations:",
                "        required: true",
                "        type: string",
                "      economy:",
                "        required: true",
                "        type: boolean",
                "      use_both:",
                "        required: true",
                "        type: boolean",
                "",
                "permissions:",
                "  actions: read",
                "  copilot-requests: write",
                "  contents: write",
                "  issues: write",
                "  pull-requests: write",
                "",
                "jobs:",
                "  flexfactor:",
                "    uses: buckeye7066/flexfactor/.github/workflows/mobile-run.yml@" + ENGINE_REF,
                "    with:",
                "      request_id: ${{ inputs.request_id }}",
                "      mode: ${{ inputs.mode }}",
                "      provider: ${{ inputs.provider }}",
                "      target_repository: ${{ github.repository }}",
                "      target_ref: ${{ inputs.target_ref }}",
                "      file: ${{ inputs.file }}",
                "      goal: ${{ inputs.goal }}",
                "      scout_apply: ${{ inputs.scout_apply }}",
                "      max_cost: ${{ inputs.max_cost }}",
                "      threshold: ${{ inputs.threshold }}",
                "      max_iterations: ${{ inputs.max_iterations }}",
                "      economy: ${{ inputs.economy }}",
                "      use_both: ${{ inputs.use_both }}",
                "    secrets:",
                "      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}",
                "      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}",
                "");
    }
}
