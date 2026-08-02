# Emit findings.json rows: RuleID/File/StartLine only, with CR/LF stripped
# from RuleID/File so downstream Markdown tables cannot be broken. Secret
# values are intentionally dropped here.
[.[] | {
  RuleID: (.RuleID | tostring | gsub("[\r\n]"; " ")),
  File: (.File | tostring | gsub("[\r\n]"; " ")),
  StartLine
}]
