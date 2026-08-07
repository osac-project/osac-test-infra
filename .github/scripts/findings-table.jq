# Emit deduplicated Markdown table rows for sanitized findings.json.
# One row per (RuleID, File): first StartLine + occurrence count.
# Used by the scan job summary (PR comments stay link-only).
# Used via: jq -r -L <dir> 'include "findings-table"; findings_table_rows'
#
# Group on raw string identities first so CR/LF sanitization cannot merge
# distinct RuleID/File values; sanitize only in the emitted row fields.
include "md-cell";

def findings_table_rows:
  map({
    RuleID: (.RuleID | tostring),
    File: (.File | tostring),
    StartLine: (try (.StartLine | tonumber) catch 0)
  })
  | group_by([.RuleID, .File])
  | map({
      RuleID: (.[0].RuleID | gsub("[\r\n]"; " ")),
      File: (.[0].File | gsub("[\r\n]"; " ")),
      StartLine: (map(.StartLine) | min),
      Count: length
    })
  | sort_by(.RuleID, .File)
  | .[]
  | "| \(.RuleID | cell) | \(.File | cell) | \(.StartLine) | \(.Count) |";
