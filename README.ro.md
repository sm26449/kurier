<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/kurier-wordmark-dark.svg">
    <img src="docs/brand/kurier-wordmark.svg" alt="Kurier" height="48">
  </picture>
</h1>

**Transport SMS pentru sisteme self-hosted, printr-un router LTE Teltonika.**
Serviciile tale trimit SMS printr-un API REST/MQTT mic; SMS-urile primite de
router ajung înapoi la ele pe MQTT sau printr-un webhook. Kurier duce mesajele
în ambele sensuri și nu le interpretează niciodată: logica alertelor și
boturile rămân în serviciile tale.

E făcut pentru momentul în care pică tot restul: când cade internetul,
Telegram și e-mailul cad odată cu el, dar un SMS prin LTE tot ajunge pe
telefon. Kurier face drumul acesta sigur: pune în coadă ce routerul nu poate
prelua, reîncearcă, oprește avalanșele și îți spune când chiar drumul SMS e
stricat.

[English](README.md)

![Pagina de stare Kurier: drumul SMS-ului, bugetul de trimitere, mesaje trimise și primite](docs/img/status-page.png)

*Pagina de stare (date demonstrative). [Statistici în modul întunecat](docs/img/statistics-dark.png).*

## Ce face

- **Trimite** — `POST /api/sms/send` sau MQTT `sms-gateway/send`. Numerele
  sunt aduse la E.164, textul e transliterat în GSM-7 (o alertă de 320 de
  caractere costă 3 SMS-uri în loc de 5), limite de rată cu buget separat
  pentru alertele critice, oprirea duplicatelor după cheia alertei,
  reîncercări idempotente.
- **Nu pierde un SMS acceptat** — dacă routerul nu răspunde, mesajul e
  salvat și confirmat cu `202`, apoi reîncercat până pleacă sau expiră.
- **Primește** — fiecare SMS primit de router e arhivat o dată, livrat o
  dată pe MQTT și opțional printr-un webhook (de exemplu inputul `sms_inbox`
  din Klopot), apoi șters de pe SIM. Marcajele `authorized` (expeditor
  cunoscut) și `stale` (prea vechi ca să acționezi pe el) îi lasă pe
  consumatori să decidă în siguranță.
- **Se vede** — o pagină de status (`/ui`): drumul SMS-ului verigă cu verigă
  (gateway → router → SIM → rețea → coadă), bugetul de trimitere în timp
  real, un grafic pe zile cu ce s-a trimis și ce s-a blocat, alertele cele
  mai zgomotoase și căutare în tot istoricul.

## Cum se leagă

```
  serviciile tale ──REST / MQTT──▶ ┌──────────┐ ──REST/ubus──▶ ┌──────────────┐ ──LTE──▶ 📱
  (alertare,                       │  Kurier  │                │  Teltonika   │
   automatizări)  ◀─MQTT/webhook── └──────────┘ ◀──gsmctl───── │ router RutOS │ ◀─LTE──  📱
                                       │                        └──────────────┘
                                MySQL (opțional): istoric, coadă,
                                arhiva SMS-urilor primite, chei de idempotență
```

## Pornire rapidă

Ai nevoie de Docker cu Compose v2 și de un router Teltonika cu modem SMS și
RutOS 7.x (testat pe RUT241), accesibil de pe host.

```bash
git clone https://github.com/sm26449/kurier.git && cd kurier
./install.sh
```

Instalatorul întreabă adresa și contul routerului, numărul tău de telefon
și cum vrei să rulezi; generează singur secretele, verifică dacă routerul
acceptă autentificarea, scrie `.env`, pornește containerele și îți propune
un SMS de test. Îl poți rula din nou oricând vrei să schimbi ceva.

Preferi manual? `cp .env.example .env`, editezi, `docker compose up -d --build`
— toți pașii sunt în [docs/INSTALL.md](docs/INSTALL.md).

Configurația implicită pornește Kurier cu un MySQL inclus (schema se creează
la prima pornire) și un backup zilnic în `./backups`. Apoi deschide **http://127.0.0.1:5080/ui** (îți cere cheia
API o singură dată).

Configurarea routerului (utilizator dedicat, permisiuni, particularități):
[docs/TELTONIKA.md](docs/TELTONIKA.md). Variantele de deploy (Traefik propriu
sau existent, TLS pentru un hostname doar din LAN):
[docs/DEPLOY.md](docs/DEPLOY.md).

## Variante de instalare

| Fișiere compose | La ce folosește |
|---|---|
| `compose.yaml` + `compose.mysql.yaml` + `compose.backup.yaml` | de sine stătător, MySQL inclus, backup zilnic (implicit) |
| `+ compose.traefik.yaml` | adaugă propriul Traefik cu Let's Encrypt (HTTP-01 sau DNS-01) și listă de IP-uri permise |
| `compose.yaml` + `compose.proxy.yaml` | în spatele unui Traefik pe care îl ai deja |
| `compose.yaml` singur | MySQL extern (`MYSQL_HOST`…) sau deloc (totul în memorie) |

Detalii, TLS pentru nume de host doar din LAN, actualizări și backup:
[docs/DEPLOY.md](docs/DEPLOY.md).

## Cum îl folosești

```json
POST /api/sms/send
{"message": "Baterie 14 % — sub pragul critic",
 "severity": "critical", "dedup_key": "soc_critical",
 "idempotency_key": "delivery-1234", "source": "alerting"}
```

Doar `message` e obligatoriu. Răspunsuri: `200` trimis · `202` pus în coadă ·
`409` duplicat · `429` limită de rată (`Retry-After`). Contractul complet,
topicurile MQTT și formatul SMS-urilor primite: [docs/API.md](docs/API.md).

## Documentație

Documentația detaliată e în engleză:
[instalare pas cu pas](docs/INSTALL.md) ·
[integrări](docs/INTEGRATIONS.md) (Home Assistant, Node-RED, Grafana…) ·
[API](docs/API.md) · [arhitectură](docs/ARCHITECTURE.md) ·
[deploy](docs/DEPLOY.md) · [router Teltonika](docs/TELTONIKA.md) ·
[operare](docs/OPERATIONS.md) · [integrare Klopot](docs/INTEGRATION_KLOPOT.md) ·
[securitate](SECURITY.md) · [istoric versiuni](CHANGELOG.md).

## Securitate

Kurier poate cheltui creditul SIM-ului. API-ul nu pornește fără `SMS_API_KEY`; nu expune
API-ul pe internet (implicit ascultă doar pe loopback, iar variantele cu
proxy adaugă o listă de IP-uri permise) și tratează SMS-urile primite ca
date nesigure. Detalii în [SECURITY.md](SECURITY.md).

## Licență

Apache License 2.0 — vezi [LICENSE](LICENSE) și [NOTICE](NOTICE).

---

## Cine duce mesajele

Kurier a fost construit de **[Stefan Maldaianu](https://github.com/sm26449)**
— proprietarul cartelei SIM, al routerului de pe raft și al telefonului care
trebuie să sune când pică tot restul — împreună cu
**[Claude](https://claude.ai)** (Anthropic), care a scris cea mai mare parte
din cod, a măsurat modemul până a recunoscut că ora lui e în UTC și încă
numără segmente SMS în somn.

Unul dintre noi hotărăște unde trebuie să ajungă mesajele. Celălalt se
asigură că ajung — chiar și când routerul are o zi proastă.
