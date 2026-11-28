-- scrub.lua - Fluent Bit filter that redacts secret-shaped values from
-- record["log"] before the line reaches Loki. Runs LAST, after labels.lua,
-- so a label built from an unredacted path/tag is untouched but the log
-- body Loki stores and Grafana renders never carries a live secret.
--
-- One Lua pattern per kind in maintenance/scrub.py's PATTERNS list, in the
-- SAME order, kept in sync by hand: maintenance/tests/test_scrub.py renders
-- every scrub.py fixture line through this file (via a `lua`/`luajit`
-- binary on PATH) and expects the identical redaction; when no such binary
-- is available it instead asserts this header lists every PATTERNS kind, in
-- order, and that each pattern string below appears in this file - so the
-- two lists cannot silently diverge either way. tests/test_fluent_bit_scrub.sh
-- additionally runs this file through a real fluent/fluent-bit container.
--
-- Lua patterns have no {n,} quantifier, no alternation, and no lookaround,
-- so each line below is the closest Lua-pattern equivalent of the Python
-- regex it reimplements (maintenance/scrub.py carries the same six rows in
-- its own header). Two Lua-side devices stand in for what Python gets from
-- the regex engine:
--
--   * `%f[%w_]` - the frontier pattern, Lua's nearest equivalent of Python's
--     left-hand `\b`: it fires only where the preceding character is not a
--     word character. Without it `sk%-` matches inside "ri[sk-]free" and
--     `st%.` matches inside "ho[st.d]ocker.internal" ([%x] accepts "d"), so
--     ordinary log text was being redacted as a secret. EVERY row below
--     carries it EXCEPT telegram-token, whose Python anchor is not `\b` but
--     `(?<![\w:-])` - a negative lookbehind over a character set that also
--     excludes ":" and "-", which no Lua pattern can express. That row is
--     therefore left unanchored on purpose, and the residual divergence is
--     over-redaction only: Lua can flag a 6-digit-plus-colon run that
--     scrub.py's lookbehind would have let through (e.g. one preceded by ":"
--     or "-"), never the other way round. Its own floor is what keeps the
--     made-up negative fixture, "telegram:-1001234567890:10", intact.
--   * `string.rep(class, N-1) .. class .. "+"` - the minimum-length floor
--     ({20,}/{30,}/{10,}), written out because Lua has no {n,}: N-1 fixed
--     repeats plus a one-or-more tail = at least N characters, the same
--     floor the Python regex states. Without it a two-character run like
--     "st.db" is a "secret".
--
--   kind            lua pattern                                python regex (maintenance/scrub.py)
--   telegram-token  %d%d%d%d%d%d+:[%w_%-]{20,}                  (?<![\w:-])\d{6,}:[A-Za-z0-9_-]{30,}(?![\w-])
--                   (no %f[%w_] on this row: Python's anchor here is the
--                   lookbehind (?<![\w:-]), not \b, and Lua has no
--                   lookaround - see the frontier bullet above. {20,} is
--                   written out below with string.rep, since Lua has no
--                   {n,} quantifier - a short suffix like ":10" must NOT
--                   match: see the "telegram negative" fixture. This row's
--                   floor is deliberately LOWER than Python's {30,}: 20 is
--                   enough to keep the negative fixture out, and a lower
--                   floor can only over-redact, never under-redact.)
--   openai-key      %f[%w_]sk%-[%w_%-]{20,}                     \bsk-[A-Za-z0-9_-]{20,}\b
--   github-token    %f[%w_]ghp_[%w]{30,}                        \b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b
--                   (one row per prefix letter p/o/u/s/r)
--                   | %f[%w_]github_pat_[%w_]{30,}
--   jwt             %f[%w_]eyJ[%w_%-]{10,}%.[%w_%-]{10,}%.[%w_%-]{10,}
--                                                               \beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b
--   infisical       %f[%w_]st%.[%x%-]{20,}                      \bst\.[a-f0-9-]{20,}\b
--   aws-key         %f[%w_]A[KS]IA[%u%d]{16}%f[^%w_]               \bA[KS]IA[0-9A-Z]{16}\b
--   slack-token     %f[%w_]xox[abprs]%-[%w%-]{10,}              \bx(?:ox[abprs]|app)-[A-Za-z0-9-]{10,}
--   google-key      %f[%w_]AIza[%w_%-]{35}                      \bAIza[0-9A-Za-z_-]{35}
--   private-key     %-%-%-%-%-BEGIN [%u ]*PRIVATE KEY%-%-%-%-%-.-%-%-%-%-%-END [%u ]*PRIVATE KEY%-%-%-%-%-
--                                                               -----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----
--   kv-secret       %f[%w_]([%w_]*KEY)=(%S+), one KEY per name   (?i)(?:(?<=^)|(?<=[^A-Za-z0-9_]))([A-Za-z0-9_]*(?:client_secret|
--                   below                                       password|passwd|token|api[_-]?key|secret))=(\S+)
--   kv-colon        `key: value` and `"key": "value"`, key containing
--                   token|secret|password|api_key (checked in code, since Lua
--                   has no alternation); an unquoted value needs 6+ chars and
--                   must not start with "<redacted:"; key and colon are kept.
--   home paths      a macOS home dir path collapses to ~/ (not a PATTERNS kind).
--
-- Neither side has a right-hand anchor on the kv row (the "=" is itself a
-- non-word character) and both keep any word-character prefix on the key:
-- `INFISICAL_CLIENT_SECRET=` and `TELEGRAM_BOT_TOKEN=` redact their values,
-- which is the point of the `[%w_]*`/`[A-Za-z0-9_]*` prefix - env-var-shaped
-- keys are how these secrets actually appear in our logs. The known cost is
-- that `mypassword=x` also redacts; over-redacting an English word is the
-- cheap direction.
--
-- kv-secret has no lookaround and no built-in case-insensitivity, so it is
-- NOT the single generic "any identifier=value" pattern the earlier draft
-- of this file used ("(%a+)=(%S+)", filtered by a KV_KEYS lookup after the
-- fact) - that swallowed a hyphenated prefix as part of the "key" (e.g.
-- `x-api-key=` read as key "x-api-key", which isn't in the lookup table, so
-- the value stayed unredacted while scrub.py's \b-anchored alternation
-- redacts just the trailing `api-key=`). Instead: one explicit,
-- case-insensitive literal pattern per key name in KV_KEYS_ORDERED (built by
-- ci_class, matching letter-by-letter with [Xx] classes), preceded by
-- `[%w_]*` for an env-var-shaped prefix and anchored on the LEFT with the
-- frontier pattern %f[%w_] - Lua's nearest equivalent to regex's \b - so it
-- fires only where the character before the run is not a word character
-- (letter/digit/underscore); the right side needs no separate anchor since
-- "=" is itself a non-word character. `client_secret` stays ahead of the
-- bare `secret` so the reported key reads the same on both sides.
--   x-api-key=abc              -> x-api-key=<redacted:kv-secret>   (hyphen prefix survives, key redacted)
--   auth-token=abc             -> auth-token=<redacted:kv-secret>  (same)
--   INFISICAL_CLIENT_SECRET=a  -> INFISICAL_CLIENT_SECRET=<redacted:kv-secret>  ([%w_]* keeps the env-var prefix)
--   TELEGRAM_BOT_TOKEN=xyz     -> TELEGRAM_BOT_TOKEN=<redacted:kv-secret>       (same)

-- Lua has no {n,} quantifier: N-1 fixed repeats of the class plus a
-- one-or-more tail is ">= N". `min(class, n)` writes that once so every
-- pattern below states its floor the same way.
local function min(class, n)
  return string.rep(class, n - 1) .. class .. "+"
end

-- telegram-token: 20 required characters after the colon (the Python side
-- requires >= 30; 20 is enough to keep the negative fixture, a 2-char
-- suffix, out, and a lower floor can only over-redact).
local TELEGRAM_PATTERN = "%d%d%d%d%d%d+:" .. min("[%w_%-]", 20)

-- The non-kv patterns, each with the %f[%w_] left frontier (Python's \b) and
-- its own minimum-length floor. Order matches scrub.py's PATTERNS.
local OPENAI_PATTERN      = "%f[%w_]sk%-" .. min("[%w_%-]", 20)
local GITHUB_GH_PATTERNS = {}
for _, letter in ipairs({ "p", "o", "u", "s", "r" }) do
  GITHUB_GH_PATTERNS[#GITHUB_GH_PATTERNS + 1] = "%f[%w_]gh" .. letter .. "_" .. min("[%w]", 30)
end
local GITHUB_PAT_PATTERN  = "%f[%w_]github_pat_" .. min("[%w_]", 30)
local JWT_PART            = min("[%w_%-]", 10)
local JWT_PATTERN         = "%f[%w_]eyJ" .. JWT_PART .. "%." .. JWT_PART .. "%." .. JWT_PART
local INFISICAL_PATTERN   = "%f[%w_]st%." .. min("[%x%-]", 20)
local AWS_PATTERN         = "%f[%w_]A[KS]IA" .. string.rep("[%u%d]", 16) .. "%f[^%w_]"
local SLACK_PATTERN       = "%f[%w_]xox[abprs]%-" .. min("[%w%-]", 10)
local SLACK_APP_PATTERN   = "%f[%w_]xapp%-" .. min("[%w%-]", 10)
local GOOGLE_PATTERN      = "%f[%w_]AIza" .. string.rep("[%w_%-]", 35)
local DASHES              = "%-%-%-%-%-"
local PEM_BEGIN           = DASHES .. "BEGIN [%u ]*PRIVATE KEY" .. DASHES
local PEM_END             = DASHES .. "END [%u ]*PRIVATE KEY" .. DASHES

-- private-key in one linear pass (a single `.-` pattern rescans the rest of
-- the line from every BEGIN): find a BEGIN, then the first END after it,
-- from that index. A BEGIN with no END stops the scan (no later BEGIN can
-- have one) and is redacted to the end of the text (a truncated record must
-- not leak the key body), exactly like scrub.py's _PemBlock.
local function scrub_pem(text)
  local out, pos, init = {}, 1, 1
  while true do
    local s, e = string.find(text, PEM_BEGIN, init)
    if not s then break end
    local _, stop = string.find(text, PEM_END, e + 1)
    out[#out + 1] = text:sub(pos, s - 1)
    out[#out + 1] = "<redacted:private-key>"
    if not stop then
      pos = #text + 1
      break
    end
    pos = stop + 1
    init = pos
  end
  if pos == 1 then return text end
  out[#out + 1] = text:sub(pos)
  return table.concat(out)
end

-- Order matters only in that a more specific key (client_secret) should be
-- tried before a key it textually contains (secret); %f[%w_] already makes
-- this safe (see header), but the explicit order keeps the intent obvious.
local KV_KEYS_ORDERED = {
  "client_secret",
  "password",
  "passwd",
  "token",
  "api_key",
  "api-key",
  "apikey",
  "secret",
}

-- Case-insensitive literal Lua pattern for a plain key name (letters,
-- digits, "_" and "-" only, which is all of KV_KEYS_ORDERED): each letter
-- becomes a [Xx] class: "-" is a Lua pattern magic character (0-or-more,
-- shortest match) outside a character class, so it is escaped as "%-";
-- every other character (here: "_") is not magic and passes through as-is.
local function ci_class(s)
  local out = {}
  for i = 1, #s do
    local c = s:sub(i, i)
    if c:match("%a") then
      out[#out + 1] = "[" .. c:lower() .. c:upper() .. "]"
    elseif c == "-" then
      out[#out + 1] = "%-"
    else
      out[#out + 1] = c
    end
  end
  return table.concat(out)
end

-- kv-colon: one pattern per keyword, the keyword inside the pattern (like
-- KV_PATTERNS), so a key without a keyword never matches and cannot swallow
-- the next key; the key must contain one of these (case-insensitive).
local COLON_WORDS = { "token", "secret", "password", "api_key", "api-key", "apikey" }
-- One run of key characters followed by a colon is found per step (the
-- frontier means one start per run, so no run is rescanned), the keyword is
-- looked up in the run with a plain find, and the value is read with an
-- anchored match at the index after the colon. COLON_QUOTED / COLON_BARE
-- return the value and the index just past it.
local COLON_HEAD = '("?)%f[%w_%-]([%w_%-]+)("?[ \t]*:[ \t]*)'
local COLON_QUOTED = '^"([^"]+)"()'
local COLON_BARE = '^([^%s,}"]+)()'
local REDACTED_OPEN = "<redacted:"

local function has_colon_word(key)
  local low = key:lower()
  for _, word in ipairs(COLON_WORDS) do
    if string.find(low, word, 1, true) then return true end
  end
  return false
end

local function scrub_colon(text)
  local out, pos, init = {}, 1, 1
  while true do
    local s, e, q, key, colon = string.find(text, COLON_HEAD, init)
    if not s then break end
    init = e + 1
    if not has_colon_word(key) then
      init = s + #q + #key
    else
      local value, after = string.match(text, COLON_QUOTED, e + 1)
      local quoted = value ~= nil
      if not quoted then value, after = string.match(text, COLON_BARE, e + 1) end
      if value and value:sub(1, #REDACTED_OPEN) ~= REDACTED_OPEN
          and (quoted or #value >= 6) then
        out[#out + 1] = text:sub(pos, s - 1)
        out[#out + 1] = q .. key .. colon
            .. (quoted and '"<redacted:kv-secret>"' or "<redacted:kv-secret>")
        pos = after
        init = after
      end
    end
  end
  if pos == 1 then return text end
  out[#out + 1] = text:sub(pos)
  return table.concat(out)
end

local KV_PATTERNS = {}
for _, key in ipairs(KV_KEYS_ORDERED) do
  -- %f[%w_]: fire only where the char before the run is NOT a word char
  -- (letter/digit/underscore) - approximates \b, and still matches a
  -- hyphenated prefix like "x-api-key=" ("-" is not a word char, so it does
  -- fire, and `[%w_]*` cannot swallow the "-").
  -- `[%w_]*` before the key name accepts an env-var-shaped prefix, so
  -- INFISICAL_CLIENT_SECRET=, TELEGRAM_BOT_TOKEN= and SOME_SERVICE_API_KEY= are
  -- caught (scrub.py's `[A-Za-z0-9_]*` does the same). It also means
  -- `mypassword=x` redacts - over-redaction, the cheap direction.
  -- The whole key (prefix included) is captured so the replacement can
  -- preserve its exact original text, like scrub.py's m.group(1) does.
  KV_PATTERNS[#KV_PATTERNS + 1] = "%f[%w_]([%w_]*" .. ci_class(key) .. ")=(%S+)"
end

function scrub_log(tag, ts, record)
  local log = record["log"]
  if type(log) ~= "string" or log == "" then
    return 0, ts, record
  end

  local changed = false
  local n

  log, n = string.gsub(log, TELEGRAM_PATTERN, "<redacted:telegram-token>")
  if n > 0 then changed = true end

  log, n = string.gsub(log, OPENAI_PATTERN, "<redacted:openai-key>")
  if n > 0 then changed = true end

  for _, pattern in ipairs(GITHUB_GH_PATTERNS) do
    log, n = string.gsub(log, pattern, "<redacted:github-token>")
    if n > 0 then changed = true end
  end
  log, n = string.gsub(log, GITHUB_PAT_PATTERN, "<redacted:github-token>")
  if n > 0 then changed = true end

  log, n = string.gsub(log, JWT_PATTERN, "<redacted:jwt>")
  if n > 0 then changed = true end

  log, n = string.gsub(log, INFISICAL_PATTERN, "<redacted:infisical>")
  if n > 0 then changed = true end

  log, n = string.gsub(log, AWS_PATTERN, "<redacted:aws-key>")
  if n > 0 then changed = true end

  log, n = string.gsub(log, SLACK_PATTERN, "<redacted:slack-token>")
  if n > 0 then changed = true end
  log, n = string.gsub(log, SLACK_APP_PATTERN, "<redacted:slack-token>")
  if n > 0 then changed = true end

  log, n = string.gsub(log, GOOGLE_PATTERN, "<redacted:google-key>")
  if n > 0 then changed = true end

  local pem = scrub_pem(log)
  if pem ~= log then changed = true end
  log = pem

  for _, pattern in ipairs(KV_PATTERNS) do
    log, n = string.gsub(log, pattern, function(key, val)
      return key .. "=<redacted:kv-secret>"
    end)
    if n > 0 then changed = true end
  end

  -- gsub counts every match, kept or not; compare text to see a real change.
  local before = log
  log = scrub_colon(log)
  if log ~= before then changed = true end

  -- Left anchor (no lookbehind in Lua): start of text or a char outside
  -- [%w_./-], so a path with the user dir below another dir is left alone.
  log, n = string.gsub(log, "^/Users/[%w._%-]+/", "~/")
  if n > 0 then changed = true end
  log, n = string.gsub(log, "([^%w_./%-])/Users/[%w._%-]+/", "%1~/")
  if n > 0 then changed = true end

  if not changed then
    return 0, ts, record
  end
  record["log"] = log
  return 2, ts, record
end
