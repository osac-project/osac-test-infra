# Shared Markdown/HTML cell escaping for gitleaks RuleID/File (and similar
# artifact-controlled strings) before they are written into tables or
# comment bodies. Used via: jq -L <dir> 'include "md-cell"; ...'
def cell:
  tostring
  | gsub("[\r\n]"; " ")
  | gsub("&"; "&amp;")
  | gsub("<"; "&lt;")
  | gsub(">"; "&gt;")
  | gsub("\\|"; "\\|");
