#!/usr/bin/env bash
# One-time setup for the security demo (tests/demo). PERSONAL/TEST tenant only - never a company tenant.
# Creates "Team A Lead" and "Team B Lead" plus one Entra group each, and appends their credentials to the git-ignored
# .env (DEMO_TEAM_<X>_UPN / _PASSWORD / _GROUP_ID / _TOTP_SECRET). Passwords are random and never printed.
# Then: register MFA (tests/demo/register_mfa.py) and map the groups in RLS (README "Security demo").
#
#   DEMO_DOMAIN=<your-tenant>.onmicrosoft.com bash tests/demo/setup_demo_users.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${DEMO_DOMAIN:?set DEMO_DOMAIN to your test tenant's domain, e.g. contoso.onmicrosoft.com}"
git check-ignore -q .env || { echo ".env is not git-ignored; refusing to write credentials"; exit 1; }
if grep -q '^DEMO_TEAM_A_UPN=' .env 2>/dev/null; then echo "demo users already in .env"; exit 0; fi

printf '\n# --- Security demo (tests/demo): test-tenant users. Never commit. ---\n' >> .env
for T in A B; do
  t=$(echo "$T" | tr 'A-Z' 'a-z')
  upn="demo-team-$t-lead@$DEMO_DOMAIN"
  pw="Dm-$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)!9"
  oid=$(az ad user create --display-name "Team $T Lead" --user-principal-name "$upn" --password "$pw" \
        --force-change-password-next-sign-in false --query id -o tsv)
  grp=$(az ad group create --display-name "finops-demo-team-$t" --mail-nickname "finops-demo-team-$t" \
        --description "Security demo: Team $T (tests/demo)" --query id -o tsv)
  az ad group member add --group "$grp" --member-id "$oid"
  printf 'DEMO_TEAM_%s_UPN=%s\nDEMO_TEAM_%s_PASSWORD=%s\nDEMO_TEAM_%s_GROUP_ID=%s\nDEMO_TEAM_%s_TOTP_SECRET=\n' \
    "$T" "$upn" "$T" "$pw" "$T" "$grp" "$T" >> .env
  echo "Team $T: user and group created"
done
