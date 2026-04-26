"""
Merkezi renk paleti.

Nasıl çalışır?
  - P: aktif palette — Rich markup'ta hex renk olarak kullanılır
  - THEME: Textual tema — CSS'de $background, $surface, $success gibi
    CSS değişkeni olarak otomatik bağlanır

Palette değiştirmek için:
  P = DARK  yerine  P = LIGHT  (veya başka bir palette)
  THEME içindeki değerleri de eşleştir.

Neden iki ayrı sistem?
  Textual CSS değişkenleri sadece Textual widget'larını (Header, Footer,
  Label, DataTable vb.) etkiler. Rich içeriği (Panel, Text, Table) Textual
  CSS'ini görmez; Python seviyesinde renk alması lazım.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.theme import Theme


@dataclass(frozen=True)
class Palette:
    bg: str           # ana arka plan
    surface: str      # kart / panel iç zemin
    border: str       # ince ayırıcı çizgiler
    border_accent: str  # mavi ton, vurgu kenarlıklar
    text: str         # birincil metin (fiyat, toplam)
    muted: str        # ikincil metin (etiketler)
    dim: str          # çok soluk (ipucu, pasif)
    positive: str     # kâr / yeşil
    negative: str     # zarar / kırmızı
    neutral: str      # sıfır PnL / gri-beyaz


DARK = Palette(
    bg="#080c12",
    surface="#0c1420",
    border="#1e2a38",
    border_accent="#1e3a5f",
    text="#f1f5f9",
    muted="#64748b",
    dim="#2d3f52",
    positive="#10b981",
    negative="#f43f5e",
    neutral="#94a3b8",
)

# Aktif palette — tek buradan değiştirerek tüm UI güncellenir
P: Palette = DARK

# Textual tema — CSS'de $background, $surface, $success, $error vb. olarak erişilir
THEME = Theme(
    name="bnc",
    primary="#3b82f6",
    secondary=DARK.positive,
    background=DARK.bg,
    surface=DARK.surface,
    error=DARK.negative,
    success=DARK.positive,
    warning="#f97316",
    accent="#60a5fa",
    foreground=DARK.text,
    variables={
        "muted": DARK.muted,
        "dim": DARK.dim,
        "border-subtle": DARK.border,
        "border-accent": DARK.border_accent,
    },
)
