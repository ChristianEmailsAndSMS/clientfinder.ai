#!/usr/bin/env bash
# Read-only security audit for the Clientfinder.ai server. Changes nothing, prints no secrets.
#   sudo /root/clientfinder.ai/deploy/security_audit.sh            # exit 1 if anything FAILs
# Run it after every deploy and monthly. See docs/SECURITY.md for what each line means and how to fix it.
set -uo pipefail

APP_DIR="${APP_DIR:-/root/clientfinder.ai}"
ENV_FILE="${ENV_FILE:-$APP_DIR/backend/.env}"
DOMAIN="${DOMAIN:-clientfinder.ai}"
SCHEME="${SCHEME:-https}"          # http only for testing against a local server
PG_CONTAINER="${PG_CONTAINER:-clientfinder_pg}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/clientfinder}"
pass=0; warn=0; fail=0
P() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
W() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; warn=$((warn+1)); }
F() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
S() { printf '  SKIP  %s\n' "$*"; }
sec() { printf '\n== %s\n' "$*"; }
envval() { [[ -r "$ENV_FILE" ]] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]$//"; }
have() { command -v "$1" >/dev/null 2>&1; }

sec "Secrets and configuration ($ENV_FILE)"
if [[ ! -r "$ENV_FILE" ]]; then F ".env not found/readable"; else
  mode=$(stat -c '%a' "$ENV_FILE"); owner=$(stat -c '%U' "$ENV_FILE")
  [[ "$mode" =~ ^(600|400)$ ]] && P ".env permissions $mode ($owner)" || F ".env is mode $mode: run chmod 600 $ENV_FILE"
  jwt=$(envval JWT_SECRET)
  if [[ -z "$jwt" || "$jwt" == "change-me-dev-only" ]]; then F "JWT_SECRET is unset or the default: admin sign-in is disabled and sessions are forgeable"
  elif (( ${#jwt} < 32 )); then F "JWT_SECRET shorter than 32 characters"; else P "JWT_SECRET set (${#jwt} chars)"; fi
  dek=$(envval DATA_ENCRYPTION_KEY)
  if (( ${#dek} >= 32 )); then P "DATA_ENCRYPTION_KEY set (2FA secrets survive a JWT_SECRET rotation)"; else W "DATA_ENCRYPTION_KEY not set: rotating JWT_SECRET would break everyone's 2FA. Set it (docs/SECURITY.md)"; fi
  [[ "$(envval DEV_FIXTURES)" =~ ^(0|false|False|)$ ]] && P "DEV_FIXTURES off (no fake data)" || F "DEV_FIXTURES is on: fake jobs would appear on the live site"
  [[ "$(envval ENABLE_DOCS)" =~ ^(1|true|True)$ ]] && F "ENABLE_DOCS is on: API docs are public" || P "API docs hidden"
  [[ "$(envval COOKIE_SECURE)" =~ ^(0|false|False)$ ]] && F "COOKIE_SECURE is off: session cookie can travel over plain http" || P "session cookie is Secure"
  [[ "$(envval GOOGLE_SCHEDULE_ENABLED)" =~ ^(1|true|True)$ ]] && W "GOOGLE_SCHEDULE_ENABLED is on: the scheduled Google plan spends YOUR SerpApi/Claude money on a timer. Leave it off so only customer-paid searches use the keys."
  em=$(envval EXTRACTION_MODEL); [[ "$em" =~ ^claude-haiku-4 ]] && W "EXTRACTION_MODEL=$em is the old, pricier model. Use claude-haiku-5-5 (or delete the line)."
  [[ -n "$(envval ADMIN_TOKEN)" ]] && W "ADMIN_TOKEN is still in .env: the old static token no longer does anything. Delete the line."
  [[ "$(envval PLAYWRIGHT_FALLBACK)" =~ ^(1|true|True)$ ]] && W "PLAYWRIGHT_FALLBACK on: Chromium opens untrusted pages. Only safe once the scraper runs as a non-root user."
  [[ "$(envval JOBS_REQUIRE_LOGIN)" =~ ^(0|false|False)$ ]] && F "JOBS_REQUIRE_LOGIN is off: the whole job database is public and scrapeable" || P "job feed requires an account"
  [[ "$(envval SIGNUP_BONUS_USD)" =~ ^0*\.?0*$ ]] && P "no free signup credit (nothing to farm)" || W "SIGNUP_BONUS_USD is set: without email verification, throwaway accounts can farm free credit"
  dburl=$(envval DATABASE_URL); dbuser=$(sed -E 's#^[a-z+]+://([^:@/]+).*#\1#' <<<"$dburl")
  if [[ -n "$dbuser" ]] && have docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PG_CONTAINER"; then
    su=$(docker exec "$PG_CONTAINER" psql -U clientfinder -d postgres -Atc "select rolsuper from pg_roles where rolname='$dbuser'" 2>/dev/null)
    [[ "$su" == "t" ]] && W "app connects to Postgres as superuser '$dbuser': create the limited role (deploy/db_least_privilege.sql)" || { [[ "$su" == "f" ]] && P "app database role '$dbuser' is not a superuser" || S "could not read role flags"; }
  else S "database role check (needs docker + the Postgres container)"; fi
fi

sec "Network exposure"
if have docker; then
  exposed=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -E '(0\.0\.0\.0|\[::\]):[0-9]+->' || true)
  [[ -z "$exposed" ]] && P "no Docker container publishes a port to the internet" || { F "Docker publishes ports publicly (bypasses ufw):"; sed 's/^/        /' <<<"$exposed"; }
else S "docker not available"; fi
if have ss; then
  other=$(ss -tlnH 2>/dev/null | awk '{print $4}' | grep -E '^(0\.0\.0\.0|\*|\[::\]):' | grep -vE ':(22|80|443)$' || true)
  [[ -z "$other" ]] && P "only 22/80/443 listen on all interfaces" || { W "other services listen on all interfaces (ufw may block them; bind to 127.0.0.1 if possible):"; sed 's/^/        /' <<<"$other"; }
fi
if have ufw; then
  u=$(ufw status verbose 2>/dev/null)
  grep -q "Status: active" <<<"$u" && P "ufw active" || F "ufw is not active"
  grep -qi "default: deny (incoming)" <<<"$u" && P "ufw default: deny incoming" || W "ufw default for incoming is not deny"
  grep -q "Nginx" <<<"$u" && W "stale ufw rule 'Nginx Full' (nginx is not installed): ufw delete allow 'Nginx Full'"
else S "ufw not installed"; fi

sec "SSH"
if have sshd; then
  cfg=$(sshd -T 2>/dev/null)
  pa=$(awk '/^passwordauthentication /{print $2}' <<<"$cfg"); pr=$(awk '/^permitrootlogin /{print $2}' <<<"$cfg")
  [[ "$pa" == "no" ]] && P "SSH password login disabled" || F "SSH allows password login: set PasswordAuthentication no (after confirming your key works)"
  [[ "$pr" == "no" || "$pr" == "prohibit-password" || "$pr" == "without-password" ]] && P "SSH root login: $pr" || F "SSH allows root password login"
else S "sshd config not readable"; fi
if systemctl is-active --quiet fail2ban 2>/dev/null; then P "fail2ban running"; else W "fail2ban not running: apt install fail2ban (blocks SSH brute force)"; fi

sec "Patching"
if systemctl is-enabled --quiet unattended-upgrades 2>/dev/null; then P "unattended security upgrades enabled"; else W "unattended-upgrades not enabled: apt install unattended-upgrades"; fi
if have apt; then n=$(apt list --upgradable 2>/dev/null | grep -ci security || true); (( n == 0 )) && P "no pending security updates" || W "$n pending security updates: apt upgrade"; fi
[[ -f /var/run/reboot-required ]] && W "reboot required to finish updates"
pa_bin="$APP_DIR/backend/.venv/bin/pip-audit"
if [[ -x "$pa_bin" ]]; then
  if out=$("$pa_bin" -r "$APP_DIR/backend/requirements.txt" --progress-spinner off 2>&1); then P "Python dependencies: no known vulnerabilities"; else F "pip-audit found problems:"; sed 's/^/        /' <<<"$out" | head -12; fi
else S "pip-audit not installed (backend/.venv/bin/pip install pip-audit)"; fi

sec "Services"
for unit in clientfinder-api clientfinder-scheduler; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    systemctl is-active --quiet "$unit" && P "$unit running" || F "$unit is not running"
    user=$(systemctl show -p User --value "$unit" 2>/dev/null)
    [[ -n "$user" && "$user" != "root" ]] && P "$unit runs as $user" || W "$unit runs as root (a bug in it is a full-server compromise): see docs/SECURITY.md 'non-root user'"
  else S "$unit not installed"; fi
done

sec "Backups"
if systemctl is-active --quiet clientfinder-backup.timer 2>/dev/null; then P "backup timer active"; else F "backup timer is not active"; fi
if [[ -d "$BACKUP_DIR" ]]; then
  m=$(stat -c '%a' "$BACKUP_DIR"); [[ "$m" == "700" ]] && P "backup dir is private (700)" || F "backup dir mode $m (should be 700): contains customer data"
  newest=$(ls -1t "$BACKUP_DIR"/clientfinder-*.dump 2>/dev/null | head -1)
  if [[ -n "$newest" ]]; then age=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 )); (( age <= 30 )) && P "newest backup is ${age}h old" || F "newest backup is ${age}h old"; else F "no backups found"; fi
else F "backup dir missing"; fi
W "backups live on this server only: set up an encrypted off-server copy (docs/SECURITY.md)"

sec "Public site ($SCHEME://$DOMAIN)"
if have curl; then
  code() { curl -s -o /dev/null -m 15 -w '%{http_code}' "$SCHEME://$DOMAIN$1" 2>/dev/null; }
  hdr() { curl -sI -m 15 "$SCHEME://$DOMAIN/health" 2>/dev/null | tr -d '\r'; }
  if [[ "$(code /health)" != "200" ]]; then
    F "site not reachable at $SCHEME://$DOMAIN/health (is the API up and Caddy routing the domain?): public checks skipped"
  else
    P "/health reachable"
    [[ "$(code /docs)" == "404" ]] && P "/docs not exposed" || F "/docs is exposed"
    [[ "$(code /openapi.json)" == "404" ]] && P "/openapi.json not exposed" || F "/openapi.json is exposed"
    c=$(code /admin/overview); [[ "$c" == "401" ]] && P "admin API refuses anonymous callers" || F "/admin/overview returned $c to an anonymous caller"
    c=$(code /admin/users); [[ "$c" == "401" ]] && P "customer list refuses anonymous callers" || F "/admin/users returned $c to an anonymous caller"
    c=$(code /jobs); [[ "$c" == "401" ]] && P "job feed refuses anonymous callers" || F "/jobs returned $c to an anonymous caller"
    c=$(code /app); [[ "$c" == "302" ]] && P "/app redirects visitors to sign in" || W "/app returned $c to an anonymous caller (expected a redirect)"
    h=$(hdr)
    for want in strict-transport-security x-content-type-options x-frame-options; do grep -qi "^$want:" <<<"$h" && P "header $want present" || F "header $want missing"; done
    grep -qi "^server:" <<<"$h" && W "Server header is sent (harmless; hide it with 'header -Server' in Caddy)"
    if [[ "$SCHEME" == "https" ]]; then
      [[ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://$DOMAIN/health" 2>/dev/null)" =~ ^30[1278]$ ]] && P "http redirects to https" || W "http does not redirect to https"
      if have openssl; then
        end=$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
        if [[ -n "$end" ]]; then days=$(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 )); (( days > 14 )) && P "TLS certificate valid for $days more days" || F "TLS certificate expires in $days days"; fi
      fi
    fi
  fi
else S "curl not available"; fi

sec "Recent sign-in activity"
if have docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PG_CONTAINER"; then
  q="select event, count(*) from auth_events where created_at > now() - interval '24 hours' group by 1 order by 2 desc"
  out=$(docker exec "$PG_CONTAINER" psql -U clientfinder -d clientfinder -At -F ' x ' -c "$q" 2>/dev/null)
  if [[ -z "$out" ]]; then P "no sign-in events in the last 24h"; else sed 's/^/        /' <<<"$out"
    fails=$(awk -F' x ' '$1 ~ /fail|locked/ {s+=$2} END{print s+0}' <<<"$out"); (( fails >= 20 )) && W "$fails failed sign-in events in 24h: look at the Security log in the dashboard" || P "failed sign-ins: $fails in 24h"; fi
else S "auth_events check (needs docker + the Postgres container)"; fi

printf '\n== %d passed, %d warnings, %d failed\n' "$pass" "$warn" "$fail"
(( fail == 0 ))
