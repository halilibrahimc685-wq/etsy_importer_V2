#!/usr/bin/env bash
# Yerel Mockups/ klasorunu Cloudflare R2'ye, uygulamanin bekledigi anahtar yapisiyla yukler.
#
# Beklenen bucket anahtarlari (S3_PREFIX bos ise):
#   T-shirt/1.png
#   CC T-shirt/1.png
#   Kids/1.png
#   placement.json   (kokte; uygulama Vercel'de repodan da okur, yine de yuklemeniz zarar vermez)
#
# S3_PREFIX=mockups ise anahtarlar:
#   mockups/T-shirt/1.png
# ve Vercel ortaminda S3_PREFIX=mockups ayarlanmalidir.
#
# Kullanim:
#   cd proje_koku
#   cp env.r2.example .env   # degerleri doldur
#   ./scripts/sync-mockups-to-r2.sh
#
# Gerekli: aws CLI v2, ortamda S3_ENDPOINT S3_BUCKET S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

for v in S3_ENDPOINT S3_BUCKET S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY; do
  if [[ -z "${!v:-}" ]]; then
    echo "Eksik ortam degiskeni: $v (.env veya export ile tanimlayin)" >&2
    exit 1
  fi
done

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI bulunamadi." >&2
  exit 1
fi

MOCKUPS_DIR="${MOCKUPS_DIR:-$REPO_ROOT/Mockups}"
if [[ ! -d "$MOCKUPS_DIR" ]]; then
  echo "Mockups klasoru yok: $MOCKUPS_DIR" >&2
  exit 1
fi

PREFIX_RAW="${S3_PREFIX:-}"
PREFIX="${PREFIX_RAW#/}"
PREFIX="${PREFIX%/}"
if [[ -n "$PREFIX" ]]; then
  DEST="s3://${S3_BUCKET}/${PREFIX}/"
else
  DEST="s3://${S3_BUCKET}/"
fi

export AWS_DEFAULT_REGION="${S3_REGION:-${AWS_DEFAULT_REGION:-auto}}"

echo "Kaynak : ${MOCKUPS_DIR}/"
echo "Hedef  : ${DEST}"
echo "Endpoint: ${S3_ENDPOINT}"
echo ""
echo "Not: Kaynakta 'Mockups/' segmenti bucket anahtarina yazilmaz; alt klasor adlari (T-shirt, Kids, ...) kokte kalir."

aws s3 sync "${MOCKUPS_DIR}/" "${DEST}" \
  --endpoint-url "${S3_ENDPOINT}" \
  --exclude ".DS_Store" \
  --exclude "Thumbs.db" \
  --exclude "*.tmp"

echo ""
echo "Tamam. Vercel'de S3_PREFIX degerinin ('${PREFIX_RAW}') bu yukleme ile eslestiginden emin olun."
