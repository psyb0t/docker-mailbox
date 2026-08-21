#!/bin/bash
# Security scan for docker-mailbox. Runs semgrep, bandit and pip-audit IN
# PARALLEL and merges their findings into sec.sarif for the GitHub Security tab.
# It NEVER fails the build: findings are reported (the pipeline uploads the
# SARIF), not gated, so a fresh advisory does not block a release. Triage the
# Security tab, then fix.
set -uo pipefail

sarif_out="${SARIF_OUT:-sec.sarif}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# shellcheck disable=SC2016 # `$schema` is a literal SARIF field name.
empty_sarif='{"version":"2.1.0","$schema":"https://json.schemastore.org/sarif-2.1.0.json","runs":[]}'

# --- run the three scanners in parallel -------------------------------------

# semgrep: SAST over the app source, native SARIF. No --error, so findings do
# not set a non-zero exit.
(semgrep scan --config p/python --config p/security-audit \
	--sarif --output "$work/semgrep.sarif" --metrics=off src \
	>"$work/semgrep.log" 2>&1 || true) &

# bandit: Python SAST, SARIF via bandit-sarif-formatter.
(bandit -r src -f sarif -o "$work/bandit.sarif" \
	>"$work/bandit.log" 2>&1 || true) &

# pip-audit: CVEs in the production deps. Resolve the locked production graph
# with uv, which honors the [tool.uv] exclude-newer gate. pip-audit does not
# consume uv.lock directly. Emit JSON, converted to SARIF below.
(
	if uv pip compile pyproject.toml --quiet -o "$work/reqs.txt" 2>"$work/uv.log"; then
		pip-audit -r "$work/reqs.txt" --format json --output "$work/pip-audit.json" \
			>"$work/pip-audit.log" 2>&1 || true
	fi
) &

wait

# --- pip-audit JSON -> SARIF ------------------------------------------------
if [ -s "$work/pip-audit.json" ]; then
	jq '{
		version: "2.1.0",
		"$schema": "https://json.schemastore.org/sarif-2.1.0.json",
		runs: [{
			tool: {driver: {name: "pip-audit", rules: []}},
			results: [(.dependencies // [])[]? | .name as $n | .version as $v
				| (.vulns // [])[] | {
					ruleId: .id,
					level: "warning",
					message: {text: ($n + " " + $v + ": " + (.description // .id)
						+ (if ((.fix_versions // []) | length) > 0
							then " (fixed in " + ((.fix_versions // []) | join(", ")) + ")"
							else "" end))},
					locations: [{physicalLocation: {
						artifactLocation: {uri: "uv.lock"},
						region: {startLine: 1}}}]
				}]
		}]
	}' "$work/pip-audit.json" >"$work/pip-audit.sarif" 2>/dev/null ||
		printf '%s' "$empty_sarif" >"$work/pip-audit.sarif"
fi

# --- any missing scanner output becomes an empty SARIF ----------------------
for f in semgrep bandit pip-audit; do
	[ -s "$work/$f.sarif" ] || printf '%s' "$empty_sarif" >"$work/$f.sarif"
done

# --- merge every SARIF into one ---------------------------------------------
jq -s '{
	version: "2.1.0",
	"$schema": "https://json.schemastore.org/sarif-2.1.0.json",
	runs: (map(.runs // []) | add)
}' "$work/semgrep.sarif" "$work/bandit.sarif" "$work/pip-audit.sarif" >"$sarif_out"

count="$(jq '[.runs[].results[]?] | length' "$sarif_out" 2>/dev/null || echo '?')"
echo "sec: merged $count finding(s) into $sarif_out (reported to the Security tab, not gating the build)"
exit 0
