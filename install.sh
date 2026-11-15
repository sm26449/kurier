#!/usr/bin/env bash
# install.sh — set Kurier up (or reconfigure it) by answering a few questions.
#
#   ./install.sh                  # the essentials: router, phone number, how to run it
#   ./install.sh --advanced       # …then every other setting, one by one
#   ./install.sh --no-start       # write .env only, don't start anything
#   ./install.sh --yes KEY=value… # no questions: values from the arguments,
#                                 #   then the existing .env, then the defaults
#   ./install.sh --skip-router-check
#
# It writes .env from .env.example (your current .env is read first and
# saved as .env.bak-<time>), generates the secrets you don't have yet,
# checks that the router accepts the login, and starts the containers.
# Run it again any time: your current values are the defaults, and settings
# added by a newer version are filled in.
#
# Nothing here is magic — it produces the same .env that docs/INSTALL.md
# describes by hand.
set -euo pipefail

(( BASH_VERSINFO[0] >= 4 )) || { echo "install.sh needs bash 4 or newer" >&2; exit 1; }
cd "$(dirname "$0")"

ADVANCED=0; YES=0; START=1; ROUTER_CHECK=1
declare -A ARG=()
for a in "$@"; do
  case "$a" in
    --advanced) ADVANCED=1 ;;
    --yes) YES=1 ;;
    --no-start) START=0 ;;
    --skip-router-check) ROUTER_CHECK=0 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    [A-Z]*=*) ARG["${a%%=*}"]="${a#*=}" ;;
    *) echo "unknown option: $a (try --help)" >&2; exit 2 ;;
  esac
done

if [[ -t 1 ]]; then B=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; N=$'\e[0m'
else B=""; DIM=""; RED=""; GRN=""; YEL=""; N=""; fi
step() { printf '\n%s== %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s  ✓ %s%s\n' "$GRN" "$*" "$N"; }
warn() { printf '%s  ! %s%s\n' "$YEL" "$*" "$N"; }
die()  { printf '%s  ✗ %s%s\n' "$RED" "$*" "$N" >&2; exit 1; }
note() { printf '%s    %s%s\n' "$DIM" "$*" "$N"; }

# ── Settings: .env.example is the template and the source of defaults ─────
[[ -f .env.example ]] || die "run this from the Kurier checkout (.env.example not found)"
declare -A DEF=() HELP=() CUR=() V=() ASKED=()
ORDER=()            # uncommented keys of .env.example, in file order
help=""
while IFS= read -r line; do
  if [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
    ORDER+=("${BASH_REMATCH[1]}"); DEF["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
    HELP["${BASH_REMATCH[1]}"]="$help"; help=""
  elif [[ "$line" == \#* && ! "$line" =~ ^#\ ?[A-Z][A-Z0-9_]*= && ! "$line" =~ ── ]]; then
    line="${line#\#}"; help+="${line# }"$'\n'
  else
    help=""
  fi
done < .env.example

unquote() {  # a .env value → the plain value
  local v="$1"
  if [[ "$v" == \'*\' && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"; v="${v//\\\'/\'}"
  elif [[ "$v" == \"*\" && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"; v="${v//\\\"/\"}"; fi
  printf '%s' "$v"
}
quote() {    # the plain value → safe in .env (Compose expands $ unless single-quoted)
  local v="$1"
  if [[ "$v" =~ ^[A-Za-z0-9_@%+=:,./-]*$ ]]; then printf '%s' "$v"
  else printf "'%s'" "${v//\'/\\\'}"; fi
}

HAVE_ENV=0
if [[ -f .env ]]; then
  HAVE_ENV=1
  while IFS= read -r line; do
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] && CUR["${BASH_REMATCH[1]}"]="$(unquote "${BASH_REMATCH[2]}")"
  done < .env
fi

# Value in force: this run's answer, else the argument, the current .env, the template.
get() {
  local k="$1"
  if [[ -v V[$k] ]]; then printf '%s' "${V[$k]}"
  elif [[ -v ARG[$k] ]]; then printf '%s' "${ARG[$k]}"
  elif [[ -v CUR[$k] ]]; then printf '%s' "${CUR[$k]}"
  else printf '%s' "${DEF[$k]:-}"; fi
}

# ask KEY "question" [default] [secret] — Enter keeps the default.
ask() {
  local k="$1" q="$2" d="${3-$(get "$1")}" secret="${4:-}" ans shown
  ASKED[$k]=1
  if (( YES )); then V[$k]="$d"; return; fi
  shown="$d"; [[ -n "$secret" && -n "$d" ]] && shown="keep current"
  if [[ -n "$secret" ]]; then
    read -r -s -p "  $q${shown:+ [$shown]}: " ans; echo
  else
    read -r -p "  $q${shown:+ [$shown]}: " ans
  fi
  V[$k]="${ans:-$d}"
}
# choose "question" default-number "option 1" "option 2" … → sets CHOICE
choose() {
  local q="$1" d="$2" i ans; shift 2
  if (( YES )); then CHOICE="$d"; return; fi
  echo "  $q"
  i=1; for o in "$@"; do echo "    $i) $o"; i=$((i + 1)); done
  while true; do
    read -r -p "  Choice [$d]: " ans; ans="${ans:-$d}"
    [[ "$ans" =~ ^[0-9]+$ ]] && (( ans >= 1 && ans <= $# )) && { CHOICE="$ans"; return; }
  done
}
confirm() {  # confirm "question" default(y|n)
  local ans
  if (( YES )); then [[ "$2" == y ]]; return; fi
  read -r -p "  $1 [$([[ "$2" == y ]] && echo Y/n || echo y/N)]: " ans
  ans="${ans:-$2}"; [[ "$ans" == [yY]* ]]
}
secret_hex() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# ── 0. Preflight ──────────────────────────────────────────────────────────
step "Kurier setup"
command -v docker >/dev/null || die "Docker is not installed (https://docs.docker.com/engine/install/)"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing ('docker compose version' fails)"
docker info >/dev/null 2>&1 || die "cannot talk to Docker — is it running, and may this user use it? (try: sudo ./install.sh)"
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), Compose $(docker compose version --short 2>/dev/null)"
HAVE_CURL=1; command -v curl >/dev/null || { HAVE_CURL=0; warn "curl not found — the router and health checks are skipped"; }
if (( HAVE_ENV )); then note "found an existing .env — its values are the defaults below"; fi

# Another Kurier compose project on this host, run from a different
# directory, would be taken over by `docker compose up` here.
PROJECT="${COMPOSE_PROJECT_NAME:-$(sed -n 's/^name: *//p' compose.yaml | head -1)}"
OTHER="$(docker compose ls -a --format json 2>/dev/null | tr '{' '\n' \
         | grep -F "\"Name\":\"$PROJECT\"" | grep -vF "$PWD/" || true)"
[[ -z "$OTHER" ]] || die "a compose project named '$PROJECT' already runs from another directory on this host.
    Reconfigure it there (its own .env), or stop it first — starting from here would replace it."

# ── 1. Router ─────────────────────────────────────────────────────────────
step "1/5  Router (Teltonika RutOS)"
note "The router's address and a login that may send SMS (the admin account works)."
ask TELTONIKA_HOST "Router address"
ask TELTONIKA_USER "Router user"
ask TELTONIKA_PASS "Router password" "$([[ "$(get TELTONIKA_PASS)" == change-me ]] || get TELTONIKA_PASS)" secret
[[ -n "${V[TELTONIKA_PASS]}" ]] || die "the router password is required"
V[TELTONIKA_SCHEME]="$(get TELTONIKA_SCHEME)"; ASKED[TELTONIKA_SCHEME]=1

json_str() { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '"%s"' "$s"; }
# router_post scheme path  (JSON body on stdin, so the password never shows in `ps`)
router_post() {
  curl -sk -m 8 -o "$TMP/body" -w '%{http_code}' -X POST "$1://${V[TELTONIKA_HOST]}$2" \
       -H 'Content-Type: application/json' --data-binary @- 2>/dev/null || true
}
check_router() {
  local scheme code login ubus
  login="{\"username\":$(json_str "${V[TELTONIKA_USER]}"),\"password\":$(json_str "${V[TELTONIKA_PASS]}")}"
  # A scheme you chose is tried first; on a first install, https before http
  # (over http the router password crosses the LAN in clear text).
  local first="${V[TELTONIKA_SCHEME]}"
  [[ -v CUR[TELTONIKA_SCHEME] || -v ARG[TELTONIKA_SCHEME] ]] || first=https
  for scheme in "$first" https http; do
    code="$(printf '%s' "$login" | router_post "$scheme" /api/login)"
    [[ "$code" == 000 || "$code" == 30[1278] ]] || break
  done
  case "$code" in
    200) if grep -q '"token"' "$TMP/body"; then
           V[TELTONIKA_SCHEME]="$scheme"; ok "login accepted ($scheme://${V[TELTONIKA_HOST]}) — sending will work"
         else warn "the router answered but refused the login — check user and password"; return 1; fi ;;
    000|30?) warn "no answer from ${V[TELTONIKA_HOST]} on http or https — wrong address, or the router is off"; return 1 ;;
    401|403) warn "the router refused the login (HTTP $code) — check user and password"; return 1 ;;
    *) warn "unexpected answer from the router (HTTP $code) — is this a Teltonika RutOS 7 router?"; return 1 ;;
  esac
  ubus="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"call\",\"params\":[\"00000000000000000000000000000000\",\"session\",\"login\",$login]}"
  printf '%s' "$ubus" | router_post "$scheme" /ubus >/dev/null
  if grep -Eq '"ubus_rpc_session": *"[0-9a-f]{32}"' "$TMP/body" \
     && ! grep -Eq '"ubus_rpc_session": *"0{32}"' "$TMP/body"; then
    ok "JSON-RPC answers — receiving SMS and modem status will work"
  else
    warn "JSON-RPC is not available: Kurier will SEND, but not receive SMS or show the modem status."
    note "Router WebUI: System → Administration → Access Control → General → Enable JSON-RPC"
    note "(firmware 7.18+: install the JSON-RPC package from the Package Manager first)."
    note "You can turn it on later, then: docker compose restart kurier"
  fi
}
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
if (( ROUTER_CHECK && HAVE_CURL )); then
  until check_router; do
    (( YES )) && { warn "continuing anyway (--yes): SMS will be queued until the router answers"; break; }
    confirm "Enter the router details again?" y || { warn "continuing: SMS will be queued until the router answers"; break; }
    ask TELTONIKA_HOST "Router address" "${V[TELTONIKA_HOST]}"
    ask TELTONIKA_USER "Router user" "${V[TELTONIKA_USER]}"
    ask TELTONIKA_PASS "Router password" "${V[TELTONIKA_PASS]}" secret
  done
fi

# ── 2. Phone numbers ──────────────────────────────────────────────────────
step "2/5  Who receives the SMS"
note "International format, e.g. +40712345678; several separated by commas."
note "SMS sent without a number go here, and only SMS *from* these numbers count as authorized."
while true; do
  ask SMS_DEFAULT_RECIPIENTS "Phone number(s)"
  V[SMS_DEFAULT_RECIPIENTS]="${V[SMS_DEFAULT_RECIPIENTS]// /}"
  bad=""
  IFS=',' read -r -a nums <<<"${V[SMS_DEFAULT_RECIPIENTS]}"
  for n in "${nums[@]}"; do [[ "$n" =~ ^\+[1-9][0-9]{6,14}$ ]] || bad="$n"; done
  [[ -z "$bad" && ${#nums[@]} -gt 0 ]] && break
  if [[ -z "${V[SMS_DEFAULT_RECIPIENTS]}" ]]; then
    warn "no number: every request will have to name its recipient"
    (( YES )) && break; confirm "Leave it empty?" n && break
  else
    warn "'$bad' is not in international format (+<country><number>)"
    (( YES )) && die "invalid SMS_DEFAULT_RECIPIENTS"
  fi
  unset 'V[SMS_DEFAULT_RECIPIENTS]'
done
ask SMS_DEFAULT_COUNTRY_CODE "Country prefix for numbers written nationally (07… → +407…)"
guess_tz="$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || true)"
tz_default="$(get TZ)"; [[ "$tz_default" == UTC && -n "$guess_tz" && ! -v CUR[TZ] && ! -v ARG[TZ] ]] && tz_default="$guess_tz"
ask TZ "Time zone (statistics per day, backup time)" "$tz_default"

# ── 3. Database ───────────────────────────────────────────────────────────
step "3/5  Where history, the queue and received SMS are kept"
cf="$(get COMPOSE_FILE)"
db_default=1
if (( HAVE_ENV )); then
  if [[ "$cf" == *compose.mysql.yaml* ]]; then db_default=1
  elif [[ -n "$(get MYSQL_PASSWORD)" ]]; then db_default=2
  else db_default=3; fi
fi
[[ -v ARG[DATABASE] ]] && case "${ARG[DATABASE]}" in bundled) db_default=1 ;; external) db_default=2 ;; none) db_default=3 ;; esac
choose "Database" "$db_default" \
  "MySQL in its own container, with a daily backup (recommended)" \
  "A MySQL server I already run" \
  "None — everything in memory, lost on restart"
DB="$CHOICE"
case "$DB" in
  1) V[MYSQL_HOST]=mysql
     V[MYSQL_PASSWORD]="$(get MYSQL_PASSWORD)";           [[ -n "${V[MYSQL_PASSWORD]}" ]] || V[MYSQL_PASSWORD]="$(secret_hex)"
     V[MYSQL_ROOT_PASSWORD]="$(get MYSQL_ROOT_PASSWORD)"; [[ -n "${V[MYSQL_ROOT_PASSWORD]}" ]] || V[MYSQL_ROOT_PASSWORD]="$(secret_hex)"
     ok "MySQL passwords: $([[ -v CUR[MYSQL_PASSWORD] && -n "${CUR[MYSQL_PASSWORD]}" ]] && echo kept || echo generated)" ;;
  2) ask MYSQL_HOST "MySQL host (container name or address)" "$([[ "$(get MYSQL_HOST)" == mysql ]] || get MYSQL_HOST)"
     ask MYSQL_PORT "MySQL port"
     ask MYSQL_DATABASE "Database name"
     ask MYSQL_USER "MySQL user (needs SELECT, INSERT, UPDATE, DELETE)"
     ask MYSQL_PASSWORD "Its password" "$(get MYSQL_PASSWORD)" secret
     [[ -n "${V[MYSQL_HOST]}" && -n "${V[MYSQL_PASSWORD]}" ]] || die "host and password are required for an existing MySQL"
     V[MYSQL_ROOT_PASSWORD]=""
     note "Create the database, the user and the tables first: docs/DEPLOY.md, \"Database\"." ;;
  3) [[ -n "$(get MYSQL_PASSWORD)" ]] && note "the MySQL passwords leave .env (they stay in the .env.bak file written below)"
     V[MYSQL_PASSWORD]=""; V[MYSQL_ROOT_PASSWORD]="" ;;
esac
for k in MYSQL_HOST MYSQL_PORT MYSQL_DATABASE MYSQL_USER MYSQL_PASSWORD MYSQL_ROOT_PASSWORD; do ASKED[$k]=1; done

# ── 4. Access ─────────────────────────────────────────────────────────────
step "4/5  Who can reach the API and the status page"
acc_default=1
if (( HAVE_ENV )); then
  if   [[ "$cf" == *compose.traefik.yaml* ]]; then acc_default=4
  elif [[ "$cf" == *compose.proxy.yaml* ]];   then acc_default=3
  elif [[ "$(get KURIER_BIND)" != 127.0.0.1 ]]; then acc_default=2; fi
fi
[[ -v ARG[ACCESS] ]] && case "${ARG[ACCESS]}" in local) acc_default=1 ;; lan) acc_default=2 ;; proxy) acc_default=3 ;; traefik) acc_default=4 ;; esac
choose "Access" "$acc_default" \
  "This machine only (127.0.0.1) — and containers on the same Docker network" \
  "Other machines on my network too (plain HTTP on a port, API key required)" \
  "Through a Traefik I already run (HTTPS, hostname)" \
  "With its own Traefik and a Let's Encrypt certificate (HTTPS, hostname)"
ACCESS="$CHOICE"
case "$ACCESS" in
  1) V[KURIER_BIND]=127.0.0.1 ;;
  2) V[KURIER_BIND]=0.0.0.0; note "Never forward this port from the internet." ;;
  3) V[KURIER_BIND]="$(get KURIER_BIND)"
     ask KURIER_HOSTNAME "Hostname (e.g. kurier.example.com)"
     while true; do
       ask PROXY_NETWORK "Docker network of that Traefik"
       docker network inspect "${V[PROXY_NETWORK]}" >/dev/null 2>&1 && break
       warn "no Docker network named '${V[PROXY_NETWORK]}' (see: docker network ls)"
       (( YES )) && die "PROXY_NETWORK does not exist"
     done
     ask TRAEFIK_CERTRESOLVER "Its certificate resolver (empty if it has a default certificate)"
     note "File-provider Traefik? Copy deploy/traefik/kurier.example.yml into its dynamic directory." ;;
  4) V[KURIER_BIND]="$(get KURIER_BIND)"
     ask KURIER_HOSTNAME "Hostname (e.g. kurier.example.com)"
     ask ACME_EMAIL "E-mail for Let's Encrypt"
     tls_default=1; [[ "$(get TLS_MODE)" == http01 ]] && tls_default=2
     choose "Certificate challenge" "$tls_default" \
       "DNS-01 through Cloudflare — works for a LAN-only hostname (recommended)" \
       "HTTP-01 — the hostname must be reachable from the internet on port 80"
     if [[ "$CHOICE" == 1 ]]; then
       V[TLS_MODE]=dns01
       ask CF_DNS_API_TOKEN "Cloudflare API token (Zone:DNS:Edit)" "$(get CF_DNS_API_TOKEN)" secret
     else V[TLS_MODE]=http01; fi
     [[ -n "${V[KURIER_HOSTNAME]}" && -n "${V[ACME_EMAIL]}" ]] || die "hostname and e-mail are required" ;;
esac
ASKED[KURIER_BIND]=1
(( ACCESS >= 3 )) && [[ -z "${V[KURIER_HOSTNAME]}" ]] && die "the hostname is required"

files=compose.yaml
[[ "$DB" == 1 ]] && files+=":compose.mysql.yaml"
[[ "$DB" != 3 ]] && files+=":compose.backup.yaml"
[[ "$ACCESS" == 3 ]] && files+=":compose.proxy.yaml"
[[ "$ACCESS" == 4 ]] && files+=":compose.traefik.yaml"
# Site-local overlays (compose.local.yaml, …) already in COMPOSE_FILE stay.
IFS=':' read -r -a old_files <<<"$cf"
for f in "${old_files[@]}"; do
  case "$f" in compose.yaml|compose.mysql.yaml|compose.backup.yaml|compose.proxy.yaml|compose.traefik.yaml|"") ;;
    *) [[ -f "$f" ]] && files+=":$f" ;; esac
done
V[COMPOSE_FILE]="$files"; ASKED[COMPOSE_FILE]=1

# ── 5. MQTT + image ───────────────────────────────────────────────────────
step "5/5  MQTT (optional)"
note "Lets other services send SMS and receive incoming ones over MQTT. Empty = off."
ask MQTT_BROKER "MQTT broker host"
if [[ -n "${V[MQTT_BROKER]}" ]]; then
  ask MQTT_PORT "MQTT port"
  ask MQTT_USER "MQTT user (empty if none)"
  [[ -n "${V[MQTT_USER]}" ]] && ask MQTT_PASS "MQTT password" "$(get MQTT_PASS)" secret
  note "Anyone who may publish on sms-gateway/send can send SMS — restrict it with broker ACLs."
fi
for k in MQTT_PORT MQTT_USER MQTT_PASS; do ASKED[$k]=1; done

V[SMS_API_KEY]="$(get SMS_API_KEY)"; ASKED[SMS_API_KEY]=1
if [[ -z "${V[SMS_API_KEY]}" ]]; then V[SMS_API_KEY]="$(secret_hex)"; ok "API key generated"; fi

# Published image if this version is on the registry, else build from the checkout.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/__init__.py 2>/dev/null || true)"
PUBLISHED="ghcr.io/sm26449/kurier:$VERSION"
V[KURIER_IMAGE]="$(get KURIER_IMAGE)"
if [[ -z "${V[KURIER_IMAGE]}" && -n "$VERSION" ]] && ! (( YES )) \
   && timeout 15 docker manifest inspect "$PUBLISHED" >/dev/null 2>&1; then
  confirm "Use the published image $PUBLISHED instead of building it here?" y && V[KURIER_IMAGE]="$PUBLISHED"
fi

# ── Advanced: everything else, one by one ─────────────────────────────────
if (( ADVANCED && ! YES )); then
  step "Advanced settings (Enter keeps the value)"
  for k in "${ORDER[@]}"; do
    [[ -v ASKED[$k] ]] && continue
    printf '\n'; [[ -n "${HELP[$k]}" ]] && printf '%s' "${HELP[$k]}" | sed "s/^/  ${DIM}# /; s/\$/${N}/"
    ask "$k" "$k"
  done
fi

# ── Write .env ────────────────────────────────────────────────────────────
step "Writing .env"
declare -A WRITTEN=()
out="$TMP/env"
{
  while IFS= read -r line; do
    if [[ "$line" =~ ^([A-Z][A-Z0-9_]*)= ]]; then
      k="${BASH_REMATCH[1]}"; WRITTEN[$k]=1; printf '%s=%s\n' "$k" "$(quote "$(get "$k")")"
    elif [[ "$line" =~ ^#\ ([A-Z][A-Z0-9_]*)= ]] && k="${BASH_REMATCH[1]}" && [[ ! -v WRITTEN[$k] ]] \
         && { [[ -v V[$k] ]] || [[ -v CUR[$k] ]] || [[ -v ARG[$k] ]]; } && [[ -n "$(get "$k")" ]]; then
      WRITTEN[$k]=1; printf '%s=%s\n' "$k" "$(quote "$(get "$k")")"
    else
      printf '%s\n' "$line"
    fi
  done < .env.example
  extra=0
  for k in "${!CUR[@]}" "${!V[@]}" "${!ARG[@]}"; do
    [[ -v WRITTEN[$k] || -z "$(get "$k")" || "$k" == DATABASE || "$k" == ACCESS ]] && continue
    (( extra )) || { printf '\n# ── Not in .env.example (kept from your previous .env) ─────────────────────\n'; extra=1; }
    WRITTEN[$k]=1; printf '%s=%s\n' "$k" "$(quote "$(get "$k")")"
  done
} > "$out"
if (( HAVE_ENV )); then
  bak=".env.bak-$(date +%Y%m%d-%H%M%S)"; cp -p .env "$bak"; chmod 600 "$bak"; note "previous .env saved as $bak"
fi
install -m 600 "$out" .env
ok ".env written (readable by you only)"
docker compose config -q 2>"$TMP/cfg.err" || { cat "$TMP/cfg.err" >&2; die "the compose configuration does not validate — nothing was started"; }
ok "compose files: ${V[COMPOSE_FILE]}"

PORT="$(get KURIER_PORT)"; URL="http://127.0.0.1:$PORT"
summary() {
  step "Done"
  echo "  Status page   $URL/ui$([[ "$ACCESS" -ge 3 ]] && echo "   and   https://${V[KURIER_HOSTNAME]}/ui")"
  echo "  API key       ${V[SMS_API_KEY]}   (paste it into the status page; it is SMS_API_KEY in .env)"
  echo "  Send an SMS   curl -X POST $URL/api/sms/send -H 'X-API-Key: <key>' \\"
  echo "                     -H 'Content-Type: application/json' -d '{\"message\": \"Hello\"}'"
  echo "  Change things ./install.sh again, or edit .env and run: docker compose up -d"
  echo "  Next          docs/INTEGRATIONS.md (Home Assistant, Node-RED, Grafana…), docs/OPERATIONS.md"
}

if (( ! START )); then
  note "not starting anything (--no-start). When ready: docker compose up -d --build"
  summary; exit 0
fi
if ! (( YES )) && ! confirm "Start Kurier now?" y; then
  note "When ready: docker compose up -d --build"; summary; exit 0
fi

# ── Start ─────────────────────────────────────────────────────────────────
step "Starting"
if [[ -n "${V[KURIER_IMAGE]}" && "${V[KURIER_IMAGE]}" == */* ]]; then
  docker compose pull -q kurier && docker compose up -d --no-build
else
  docker compose up -d --build
fi
if (( ! HAVE_CURL )); then summary; exit 0; fi

printf '  waiting for Kurier'
health=""
for _ in $(seq 1 60); do
  health="$(curl -fsS -m 12 "$URL/api/health" 2>/dev/null || true)"
  [[ -n "$health" ]] && break
  printf '.'; sleep 2
done
echo
[[ -n "$health" ]] || die "Kurier did not answer on $URL after 2 minutes — see: docker compose logs --tail 50 kurier"
field() { printf '%s' "$health" | sed -n "s/.*\"$1\":\"\{0,1\}\([^\",}]*\).*/\1/p"; }
case "$(field status)" in
  healthy)  ok "healthy: router reachable, SIM ready (mode: $(field api_mode))" ;;
  degraded) warn "the router answers but the SIM is not ready — check Network → Mobile in the router" ;;
  *)        warn "Kurier runs, but cannot reach the router yet — SMS are queued until it can" ;;
esac
[[ "$(field api_mode)" == rest ]] && warn "mode 'rest': sending only — enable JSON-RPC on the router to receive SMS"

first="${V[SMS_DEFAULT_RECIPIENTS]%%,*}"
if [[ "$(field status)" == healthy && -n "$first" ]] && ! (( YES )) \
   && confirm "Send a test SMS to $first now?" y; then
  code="$(curl -sS -m 30 -o "$TMP/send" -w '%{http_code}' -X POST "$URL/api/sms/send" \
          -H "X-API-Key: ${V[SMS_API_KEY]}" -H 'Content-Type: application/json' \
          -d "{\"message\": \"Kurier is set up. This is a test SMS.\", \"number\": \"$first\", \"source\": \"install\"}" || true)"
  case "$code" in
    200) ok "sent — your phone should ring in a few seconds" ;;
    202) warn "queued: the router did not take it; Kurier retries. Reason: $(sed -n 's/.*"detail":"\([^"]*\)".*/\1/p' "$TMP/send")" ;;
    *)   warn "not sent (HTTP $code): $(head -c 300 "$TMP/send")" ;;
  esac
fi
summary
