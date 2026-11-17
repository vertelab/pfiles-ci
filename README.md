# pfiles-ci

Gemensam CI-grind för **p-filer** — versionsagnostisk Odoo-kod där en källfil
expanderas till rätt kod för varje utgåva.

Ett repo anropar den här workflowen i stället för att kopiera den. Skripten
ligger här, inte i det anropande repot, så en rättelse slår igenom överallt.

---

## Varför

En p-fil är källan för **alla** utgåvor:

```
crm_lead.p.py ──preprocess -D VERSION=18.0──▶ crm_lead.py  (Odoo 18)
              ──preprocess -D VERSION=20.0──▶ crm_lead.py  (Odoo 20)
```

Det ger en buggfix som når alla utgåvor på en gång — men det ger också fem
sätt att tysta gå sönder:

| Fel | Konsekvens |
|---|---|
| Utdata inte omgenererad efter en p-filändring | Odoo kör gammal kod |
| Handredigerad utdata | Skrivs över vid nästa expansion, ändringen tappas |
| Källfil utan p-motsvarighet | Filen expanderas aldrig |
| 20.0-specifik kod utan `# #if` | Fungerar på en utgåva, faller på en annan |
| p-filer som driver isär mellan grenarna | Fixar tappas när någon ändrar en annan gren |

Grinden mäter alla fem. Utan den upptäcks felen först när någon råkar titta.

### Varför centralt och inte kopior

Grinden ändrades **28 gånger på en vecka** under utvecklingen. Med 200 repon
och kopior hade det varit 5 600 synkningar — och varje missad synk är en tyst
gammal grind som ser grön ut.

Mätt 2026-11-17, innan centraliseringen:

| Status | Repon |
|---|---:|
| Komplett grind | 1 |
| Delvis (drivit isär) | 2 |
| Saknar helt | 22 |

Ett repo som fått grinden en vecka tidigare saknade redan ett av skripten.
Det är samma sjukdom som p-filerna hade.

---

## Vad den kontrollerar

Fem grindar plus en synkkontroll:

| # | Grind | Frågar | Fäller när |
|---|---|---|---|
| 1 | **Täckning** | Är varje källfil p-fil eller genererad? | Handskriven fil saknar p-källa |
| 2 | **Expansion** | Stämmer committad utdata med p-filerna? | Utdata är inaktuell (`STALE`) |
| 3 | **Syntax** | Är direktiven rätt skrivna? | `#if` i stället för `# #if` |
| 4 | **Bygge** | Kompilerar och tolkas allt? | `compileall` eller XML-parse fallerar |
| 5 | **Installation** | Går modulerna att installera i SIN utgåva? | Modulen laddas inte |
| — | **Synk** | Finns samma p-filer på alla utgåvegrenar? | Drift mellan grenarna |

Grind 1–4 körs en gång. Grind 5 körs **per utgåva** i en matris, så en trasig
20.0 inte kan gömma sig bakom en grön 18.0.

### Miljöfel skiljs från trasig modul

Grind 5 skiljer på tre utfall:

| Utfall | Betyder | Fäller? |
|---|---|---|
| **OK** | Modulen installerades | Nej |
| **MODULE** | Odoo startade, modulen kunde inte laddas | **Ja** — kodfel |
| **ENV** | Odoo/DB/beroende gick inte att få igång | Nej, men räknas och namnges |

Ett saknat beroende ska inte skicka utvecklaren att leta i sin egen modul.
Jobbet redovisar hur många av totalen som bevisades, och en utgåva där **ingen**
modul kunde bevisas faller — då har grinden inte åstadkommit något.

### Pinnad version

Anroparen **pinnar** en version. Det är avsiktligt: en ändring här slår inte
igenom förrän någon bumpar, så en ny utgåva av grinden kan provas i ett repo
i taget.

```yaml
uses: vertelab/pfiles-ci/.github/workflows/pfiles.yml@v1
```

---

## Installera i ett nytt repo

Görs lämpligen **i samband med ett porteringsarbete** — då finns ändå
anledning att röra repot, och grinden ger direkt besked om vad som är kvar.

### 1. Anroparen

Skapa `.github/workflows/pfiles.yml` i repot:

```yaml
name: pfiles

on:
  push:
    branches: ['*.0', 'ci/**', 'pfiles/**']
  pull_request:

jobs:
  pfiles:
    uses: vertelab/pfiles-ci/.github/workflows/pfiles.yml@v1
```

Det är hela filen. Inga skript kopieras — de hämtas från det här repot.

### 2. Vilka utgåvor repot har

Skapa `.pfiles-ci`:

```yaml
pfiles-ci: 1
editions: 18.0 20.0
# deps: OCA/dms odoo-base     # repon modulerna beror på, om några
```

Kontrollera att grenarna finns:

```bash
for e in 18.0 19.0 20.0; do
  git rev-parse --verify --quiet "origin/$e" >/dev/null && echo "$e finns"
done
```

En utgåva som står i filen men saknar gren är ett **konfigurationsfel** —
jobbet fäller med besked. Det är inte samma sak som en trasig modul.

`deps:` namnger repon modulerna beror på. Ett namn utan `/` antas höra till
`vertelab`; `OCA/dms` är ett fullt slug. De klonas på samma utgåvegren.

### 3. Vitlista det som ännu inte är p-filer

```bash
python3 .github/pfiles-check.py --version 18.0 --print-allow . | sudo tee .pfiles-allow
```

⚠ Skriptet finns inte i repot än — kör det från en checkout av `pfiles-ci`:

```bash
git clone --depth 1 https://github.com/vertelab/pfiles-ci /tmp/pfiles-ci
python3 /tmp/pfiles-ci/scripts/pfiles-check.py --version 18.0 --print-allow . \
  | sudo tee .pfiles-allow
```

Vitlistan ska bara **krympa**. Lägg inte till rader för ny kod — p-fila i
stället.

### 4. Koppla in den lokala förkontrollen

```bash
git config core.hooksPath .githooks
```

Hooken kommer från Salt (`odoo.pfiles`) och expanderar p-filer vid commit.
Kontrollera att den är inkopplad — `git config core.hooksPath` ska svara
`.githooks`, inte tomt.

### 5. Verifiera

```bash
git add .github/workflows/pfiles.yml .pfiles-ci .pfiles-allow
git commit -m "ci(pfiles): anslut till gemensam grind"
git push
```

Första körningen visar vad som återstår. Rött är väntat om repot inte är
färdigmigrerat — det är listan över arbetet.

---

## Underhåll av det här repot

Ändra skripten här, inte i de anropande repona.

```bash
# 1. Ändra i scripts/ eller .github/workflows/pfiles.yml
# 2. Verifiera mot ett riktigt repo innan taggen flyttas
# 3. Committa och tagga
git tag -f v1 && git push --force origin v1
```

**`v1` är en rörlig tagg** som flyttas framåt. Anropare som pinnar `@v1` får
rättelsen vid nästa körning. Vill ett repo frysa en version pekar det på en
fast tagg (`@v1.3.0`) eller en commit-SHA.

Skyddsnätet är att `odoo-crm` kör den här workflowen på varje ändring — en
trasig version syns där först.

### Vad som INTE ska hit

| Fil | Varför den hör till repot |
|---|---|
| `.pfiles-ci` | `editions:` och `deps:` skiljer per repo |
| `.pfiles-allow` | vitlistan är repots egen skuld |

---

## Filer

```
.github/workflows/pfiles.yml   reusable workflow (workflow_call)
scripts/pfiles-check.py        täckning, expansion, säkerhetsformat
scripts/pfiles-editions.py     läser .pfiles-ci, bygger matrisen
scripts/pfiles-expand.py       expanderar per utgåva till egen katalog
scripts/pfiles-install.sh      installerar en modul, skiljer ENV från MODULE
scripts/pfiles-sync.py         mäter och reparerar drift mellan grenar
```

## Se även

- `/skill:odoo-pfiles` — p-fil-mekaniken och de två arbetsflödena
- `docs/pfiles-ci.md` i `vertelab/plan` — grindarna i detalj
- `docs/pfiles-security-csv.md` — säkerhetsfilerna för 18/19 vs 20
- `/srv/salt/README.md` — hooken och `core.hooksPath`
