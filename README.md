# Passerelle commune d'envoi de SMS

Étude de cas DSF 2026 : la passerelle unique par laquelle les applications de l'État envoient leurs SMS (codes OTP et messages d'information). Elle valide, compte et limite les envois, les transmet aux opérateurs (simulés), tient compte de leurs comptes rendus signés et expose l'état de chaque SMS.

**Stack** : Python 3.10+ (testé en 3.12), Django 5.1, Django REST Framework, drf-spectacular (Swagger), SQLite. Aucun service externe à installer (pas de Redis, pas de Celery, pas de Docker).

---

## Sommaire

- [Passerelle commune d'envoi de SMS](#passerelle-commune-denvoi-de-sms)
	- [Sommaire](#sommaire)
	- [1. Démarrage](#1-démarrage)
	- [2. Clés d'API de test](#2-clés-dapi-de-test)
	- [3. Description de l'API](#3-description-de-lapi)
		- [Authentification](#authentification)
		- [Endpoints](#endpoints)
		- [Soumettre un SMS](#soumettre-un-sms)
		- [Soumettre une campagne](#soumettre-une-campagne)
		- [Erreurs](#erreurs)
		- [Cycle de vie d'un SMS](#cycle-de-vie-dun-sms)
	- [4. Simulateur d'opérateurs : obtenir chaque cas](#4-simulateur-dopérateurs--obtenir-chaque-cas)
		- [Envoyer un compte rendu à la main](#envoyer-un-compte-rendu-à-la-main)
	- [5. Tester la plage horaire sans attendre la nuit](#5-tester-la-plage-horaire-sans-attendre-la-nuit)
	- [6. Conception](#6-conception)
		- [Organisation du code](#organisation-du-code)
		- [Modèle de données](#modèle-de-données)
		- [Réponse aux incidents du premier prototype](#réponse-aux-incidents-du-premier-prototype)
		- [Choix et hypothèses](#choix-et-hypothèses)
	- [7. Tests automatisés](#7-tests-automatisés)
	- [8. Configuration](#8-configuration)
	- [9. État d'avancement](#9-état-davancement)
		- [Ce qui fonctionne (socle obligatoire complet)](#ce-qui-fonctionne-socle-obligatoire-complet)
		- [Bonus](#bonus)
		- [Limites connues](#limites-connues)

---

## 1. Démarrage

### Démarrage rapide (Linux / macOS)

```bash
git clone https://github.com/tawaliou/etude_de_cas_dsf_2026.git && cd etude_de_cas_dsf_2026
./setup.sh     # crée le venv, installe les dépendances, crée la base SQLite (migrate)
./run.sh       # démarre l'API et le worker ensemble ; Ctrl+C arrête les deux
```

`run.sh` lance `setup.sh` s'il n'a pas encore été exécuté. Options : `PORT=8001 ./run.sh`, `GATEWAY_FAKE_NOW=2026-11-04T23:30 ./run.sh` (heure simulée, voir [section 5](#5-tester-la-plage-horaire-sans-attendre-la-nuit)). Si `python3` n'est pas en 3.10+ : `PYTHON=python3.12 ./setup.sh`.

### Démarrage manuel (toutes plateformes, dont Windows)

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows : .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate             # crée db.sqlite3 et ses tables
```

L'application tourne en **deux processus** (deux terminaux, venv activé dans chacun) :

```bash
# Terminal 1 : l'API (et le point d'entrée des comptes rendus opérateurs)
python manage.py runserver

# Terminal 2 : le worker, qui transmet les SMS aux opérateurs (envois, programmations, nouvelles tentatives)
python manage.py worker
```

### Utiliser l'API

Ouvrir **http://127.0.0.1:8000/api/docs** (Swagger) :

1. cliquer sur **Authorize** et coller une clé d'API (ci-dessous) ;
2. sur un endpoint : **Try it out** puis **Execute**.

> Sans worker, les SMS sont acceptés mais restent `EN_ATTENTE` : c'est le worker qui les transmet.
> Le simulateur renvoie ses comptes rendus à `http://127.0.0.1:8000`. Si l'API tourne sur un autre port, définir `GATEWAY_BASE_URL` (voir [Configuration](#8-configuration)) pour le worker.

---

## 2. Clés d'API de test

Les applications clientes sont déclarées dans la configuration (`CLIENT_APPS` dans `config/settings.py`).

| Application | Clé d'API (en-tête `X-API-Key`) | Quota journalier |
|---|---|---|
| ETATCIVIL | `etatcivil-test-key` | 100 segments |
| COURRIER | `courrier-test-key` | 5 segments |

Vérifier une clé : `curl -H "X-API-Key: etatcivil-test-key" http://127.0.0.1:8000/api/me`

---

## 3. Description de l'API

Documentation interactive complète : **`/api/docs`** (Swagger UI), schéma OpenAPI : **`/api/schema`**.

### Authentification

- **Applications clientes** : en-tête `X-API-Key`. Sans clé ou avec une clé invalide : `401`. Une application ne voit et ne manipule **que ses propres SMS et campagnes** : l'identifiant d'une autre application répond `404` (on ne révèle pas son existence).
- **Opérateurs** (callback des comptes rendus) : pas de clé d'API, mais une signature HMAC-SHA256 du corps dans l'en-tête `X-Signature`.

### Endpoints

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/me` | Application appelante (test de la clé). |
| `POST` | `/api/sms` | Soumettre un SMS. |
| `GET` | `/api/sms` | Lister ses SMS (paginé, filtres `status`, `category`, `reference`). |
| `GET` | `/api/sms/{id}` | Consulter un SMS, avec son **historique** (transmissions, comptes rendus, nouvelles tentatives…). |
| `POST` | `/api/sms/{id}/cancel` | Annuler un SMS pas encore transmis. |
| `GET` | `/api/usage` | Consommation du jour (quota, utilisé, restant). |
| `POST` | `/api/campaigns` | Soumettre une campagne INFO (500 destinataires au plus). |
| `GET` | `/api/campaigns` | Lister ses campagnes. |
| `GET` | `/api/campaigns/{id}` | Consulter une campagne : résultat et statut actuel par destinataire. |
| `POST` | `/api/operators/{operateur}/reports` | Compte rendu de livraison envoyé par un opérateur (signé). |

### Soumettre un SMS

```bash
curl -X POST http://127.0.0.1:8000/api/sms \
  -H "X-API-Key: etatcivil-test-key" -H "Content-Type: application/json" \
  -d '{"to": "0197000000", "text": "Bonjour", "category": "INFO", "reference": "rdv-123"}'
```

| Champ | Obligatoire | Description |
|---|---|---|
| `to` | oui | 10 chiffres, commence par `01`, opérateur attribué. |
| `text` | oui | 6 segments au plus. |
| `category` | oui | `OTP` (code à usage unique, prioritaire) ou `INFO`. |
| `reference` | oui | Référence propre à l'application, garantit l'envoi unique (voir plus bas). |
| `send_at` | non | Envoi programmé (ISO 8601). Absent ou passé : envoi immédiat. |

Réponse `201` :

```json
{
  "id": "9b0c…", "reference": "rdv-123", "to": "0197000000", "operator": "MOOV",
  "category": "INFO", "text": "Bonjour", "segments": 1, "status": "EN_ATTENTE",
  "scheduled_at": null, "attempts": 0, "next_attempt_at": null,
  "created_at": "…", "updated_at": "…"
}
```

Pour un OTP, `text` vaut toujours `"****"`.

**La référence (envoi unique).** Elle est choisie par l'application et doit désigner *un message*, pas une tentative : l'application la génère une fois et la renvoie telle quelle si elle rejoue sa requête (réseau instable, timeout). Même référence + même contenu → `200` avec le SMS existant, sans nouvel envoi. Même référence + contenu différent → `409`. L'unicité est par application.

### Soumettre une campagne

```bash
curl -X POST http://127.0.0.1:8000/api/campaigns \
  -H "X-API-Key: courrier-test-key" -H "Content-Type: application/json" \
  -d '{"reference": "camp-1", "text": "Information importante",
       "recipients": ["0197000000", "0297000000", "0197000000", "0150000000", "0140000000"]}'
```

Chaque destinataire est traité séparément et la réponse donne son résultat :

```json
{
  "id": "…", "reference": "camp-1", "recipients_count": 5, "accepted_count": 3, "refused_count": 2,
  "status_summary": {"EN_ATTENTE": 3},
  "results": [
    {"to": "0197000000", "accepted": true, "sms_id": "…", "status": "EN_ATTENTE"},
    {"to": "0297000000", "accepted": false, "error": {"code": "validation_error", "message": "Le numéro doit comporter 10 chiffres et commencer par 01."}},
    {"to": "0197000000", "accepted": false, "error": {"code": "duplicate_recipient", "message": "Destinataire en double dans la campagne."}},
    {"to": "0150000000", "accepted": true, "sms_id": "…", "status": "EN_ATTENTE"},
    {"to": "0140000000", "accepted": true, "sms_id": "…", "status": "EN_ATTENTE"}
  ]
}
```

Un destinataire est accepté tant que le quota le permet, refusé ensuite (`quota_exceeded`). Un texte invalide refuse toute la campagne (`400`). Chaque SMS d'une campagne se consulte et s'annule individuellement via `/api/sms/{id}`.

### Erreurs

Toutes les erreurs ont le même format JSON, jamais une erreur interne :

```json
{"error": {"code": "quota_exceeded", "message": "Quota journalier dépassé : 4/5 segments utilisés, ce SMS en demande 2.", "details": {"used": 4, "quota": 5, "requested": 2}}}
```

| Code HTTP | Cas |
|---|---|
| `400` | Saisie invalide : numéro, opérateur non attribué, texte > 6 segments, champ manquant, filtre inconnu, compte rendu mal formé. `details` indique le champ. |
| `401` | Clé d'API absente ou invalide ; signature de compte rendu invalide. |
| `404` | SMS ou campagne inconnu, ou appartenant à une autre application ; opérateur ou message inconnu. |
| `409` | Référence réutilisée avec un autre contenu ; annulation d'un SMS déjà transmis ou définitif. |
| `429` | Quota journalier dépassé. |

### Cycle de vie d'un SMS

```
EN_ATTENTE ──► TRANSMIS ──► DELIVRE
PROGRAMME ─┘      │   └───► ECHOUE
   │              └──► EN_ATTENTE (nouvelle tentative après échec temporaire)
   └──► ANNULE  (seulement depuis EN_ATTENTE ou PROGRAMME)
```

`DELIVRE`, `ECHOUE` et `ANNULE` sont **définitifs** : un compte rendu qui arrive ensuite est ignoré (et tracé dans l'historique).

---

## 4. Simulateur d'opérateurs : obtenir chaque cas

Le simulateur se comporte comme un opérateur : il accuse réception de la transmission (ou se dit indisponible), puis rappelle la passerelle environ 1 seconde plus tard par un vrai `POST /api/operators/{op}/reports` signé en HMAC-SHA256.

**Le scénario est choisi par le dernier chiffre du numéro du destinataire.** Avec le worker lancé, soumettre un SMS à ce numéro (référence différente à chaque fois), attendre quelques secondes, puis `GET /api/sms/{id}` et regarder `status`, `attempts` et `history`.

| Cas demandé | Numéro (ex.) | Attente | Résultat attendu |
|---|---|---|---|
| SMS délivré | `0197000000` | ~2 s | `DELIVRE`, 1 tentative |
| Échec temporaire puis réussite | `0197000001` | ~5 s | `DELIVRE`, 2 tentatives (`ECHEC_TEMPORAIRE` puis `DELIVRE`) |
| Échec définitif | `0197000002` | ~2 s | `ECHOUE`, 1 tentative, aucune nouvelle tentative |
| Opérateur indisponible | `0197000003` | ~16 s | `ECHOUE` après 4 tentatives (1 + 3 nouvelles, espacées de 2, 4, 8 s) |
| Compte rendu envoyé deux fois | `0197000004` | ~3 s | `DELIVRE` ; le second compte rendu apparaît en `COMPTE_RENDU_IGNORE` |
| Signature fausse | `0197000005` | ~2 s | reste `TRANSMIS` ; historique `COMPTE_RENDU_REJETE` (réponse `401` au simulateur) |
| (en plus) Indisponible puis accepté | `0197000006` | ~4 s | `DELIVRE`, 2 tentatives |
| (en plus) Compte rendu jamais reçu | `0197000007` | — | reste `TRANSMIS` |

Les chiffres 8 et 9 se comportent comme 0. Le préfixe choisit l'opérateur : `0140…`–`0149…` CELTIIS, `0150…`–`0169…` MTN, `0190…`–`0199…` MOOV.

Exemple d'historique pour `0197000001` :

```json
"history": [
  {"kind": "TRANSMISSION", "detail": "Tentative 1 acceptée par MOOV (id MOOV-6b6126c91859)."},
  {"kind": "COMPTE_RENDU", "detail": "ECHEC_TEMPORAIRE reçu de MOOV (id MOOV-6b6126c91859)."},
  {"kind": "NOUVELLE_TENTATIVE", "detail": "Après échec temporaire : tentative 2/4 à 18:42:56 (heure du Bénin)."},
  {"kind": "TRANSMISSION", "detail": "Tentative 2 acceptée par MOOV (id MOOV-9bd8b97442db)."},
  {"kind": "COMPTE_RENDU", "detail": "DELIVRE reçu de MOOV (id MOOV-9bd8b97442db)."}
]
```

### Envoyer un compte rendu à la main

La commande `send_report` envoie à l'API (qui doit tourner) un compte rendu signé pour un SMS déjà transmis, par exemple un SMS au numéro finissant par `7` resté `TRANSMIS` :

```bash
python manage.py send_report <sms_id> DELIVRE --bad-signature   # signature fausse  → 401, rien ne change
python manage.py send_report <sms_id> DELIVRE --times 2         # en double         → 200 appliqué, puis 200 ignoré
python manage.py send_report <sms_id> ECHEC_TEMPORAIRE           # → nouvelle tentative programmée
```

Ou avec curl (secret de MOOV : `moov-secret`, l'`operator_message_id` est visible dans `GET /api/sms/{id}`) :

```bash
BODY='{"message_id": "MOOV-xxxxxxxxxxxx", "status": "DELIVRE"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac moov-secret | awk '{print $NF}')
curl -X POST http://127.0.0.1:8000/api/operators/MOOV/reports \
  -H "Content-Type: application/json" -H "X-Signature: $SIG" -d "$BODY"
```

---

## 5. Tester la plage horaire sans attendre la nuit

Les SMS INFO ne partent qu'entre **7 h (inclus) et 21 h (exclu), heure du Bénin** ; en dehors, ils sont différés à 7 h. Les OTP partent immédiatement et passent avant les INFO en attente.

La variable `GATEWAY_FAKE_NOW` (heure du Bénin, format ISO) **décale l'horloge** de la passerelle : elle démarre à cet instant puis le temps s'écoule normalement. Donner **la même valeur aux deux processus** :

```bash
# Terminal 1
GATEWAY_FAKE_NOW=2026-11-05T06:59:40 python manage.py runserver
# Terminal 2
GATEWAY_FAKE_NOW=2026-11-05T06:59:40 python manage.py worker
```

Ou, en une commande : `GATEWAY_FAKE_NOW=2026-11-05T06:59:40 ./run.sh` (la variable est transmise aux deux processus).

1. Soumettre un SMS `INFO` → statut `PROGRAMME`, `next_attempt_at` = `2026-11-05T07:00:00+01:00`, historique `DIFFERE`.
2. Soumettre un SMS `OTP` → part immédiatement.
3. Attendre ~25 secondes (7 h passe dans l'horloge simulée) → l'INFO est transmis puis `DELIVRE` (historique `DIFFERE → TRANSMISSION → COMPTE_RENDU`).

Avec `GATEWAY_FAKE_NOW=2026-11-04T23:30`, un INFO est programmé au lendemain 7 h (`next_attempt_at` = `2026-11-05T07:00:00+01:00`). Le quota du jour suit aussi l'horloge simulée.

---

## 6. Conception

### Organisation du code

```
config/                  réglages Django, URLs (Swagger)
gateway/
  domain.py              règles pures, sans base : segments, opérateur, plage horaire, empreinte du contenu
  clock.py               source unique du temps (heure du Bénin, GATEWAY_FAKE_NOW)
  auth.py                applications clientes (configuration) et authentification X-API-Key
  models.py              Sms, SmsEvent (historique), Campaign ; table des transitions de statut
  services.py            cas d'usage transactionnels : soumettre, consulter, annuler, campagnes
  dispatch.py            transmission des SMS dus et nouvelles tentatives (appelé par le worker)
  reports.py             traitement des comptes rendus (signature, statuts définitifs)
  operators.py           contrat opérateur (OperatorClient) et signature HMAC
  simulator.py           opérateurs simulés
  api/                   serializers, vues, format d'erreur
  management/commands/   worker, send_report
  tests/                 tests automatisés
```

Les vues sont fines : elles valident la saisie et appellent un service. Les règles métier testables sans base sont dans `domain.py`. La passerelle ne connaît que l'interface `OperatorClient` : brancher un vrai opérateur revient à fournir une autre classe (`OPERATOR_CLIENT`).

### Modèle de données

- **Sms** : `id` (UUID), `app_code`, `reference` (unique par application), `to`, `operator`, `category`, `text`, `content_hash` (empreinte pour l'envoi unique), `segments`, `status`, `scheduled_at` (date demandée), `next_attempt_at` (prochain envoi : programmation, plage horaire ou nouvelle tentative), `attempts`, `operator_message_id` (identifiant donné par l'opérateur à la tentative en cours), `quota_day` (jour du Bénin auquel les segments sont imputés), `campaign`.
- **SmsEvent** : historique d'un SMS (`kind`, `detail`, date). Ne contient jamais le texte.
- **Campaign** : `reference` (unique par application), `text`, `recipients_count`, `accepted_count`, `results` (résultat par destinataire, dans l'ordre reçu).
- **Applications clientes** : dans la configuration, comme demandé (pas de table).

### Réponse aux incidents du premier prototype

| Incident | Réponse |
|---|---|
| OTP reçus deux fois | Envoi unique par `reference` (contrainte d'unicité en base + empreinte du contenu). Le worker « réserve » un SMS par une mise à jour conditionnelle (`UPDATE … WHERE status IN (…)`) : un SMS n'est transmis qu'une fois par tentative. |
| Campagne qui consomme tout le crédit et bloque les OTP | Quota journalier par application, vérifié destinataire par destinataire ; les OTP sont toujours transmis avant les INFO en attente. |
| SMS notés « délivrés » alors que l'opérateur avait échoué | Le statut ne passe à `DELIVRE` que sur un compte rendu `DELIVRE` signé ; l'accusé de réception de l'opérateur ne donne que `TRANSMIS`. |
| SMS d'information reçus à 2 h du matin | Plage 7 h–21 h appliquée à la soumission **et** par le worker juste avant chaque transmission (une nouvelle tentative ou un retard qui passe 21 h est repoussé à 7 h). |
| Une application pouvait consulter les SMS d'une autre | Toutes les requêtes sont filtrées par l'application authentifiée ; `404` pour les autres. |
| OTP lisibles en clair dans les journaux | Le texte n'est jamais journalisé (ni dans `__str__`/`__repr__`) ; il est masqué (`****`) dans toutes les réponses d'un OTP. |

### Choix et hypothèses

- **Quota** : les segments sont imputés au jour (heure du Bénin) de la **soumission**, y compris pour un envoi programmé plus tard. Un SMS refusé n'est pas enregistré et ne consomme rien ; un SMS annulé rend ses segments.
- **Segments** : la longueur est comptée en caractères Unicode (`len` Python) ; un seul caractère non ASCII (accent, emoji) fait passer tout le texte à 70 caractères par segment.
- **`send_at` dans le passé** : envoi immédiat (pas de rejet).
- **Plage horaire** : 7 h inclus, 21 h exclu. Un INFO programmé à 22 h part le lendemain à 7 h.
- **Nouvelles tentatives** : « au plus 3 nouvelles tentatives » = 4 tentatives au total. Délais 2, 4, 8 s, volontairement courts pour la démonstration (configurables).
- **Compte rendu d'une tentative précédente** : chaque tentative reçoit un nouvel identifiant opérateur ; un compte rendu portant un ancien identifiant est refusé (`404`), pour qu'un vieux compte rendu ne modifie pas la tentative en cours.
- **Concurrence** : SQLite est utilisé en mode `IMMEDIATE` (chaque transaction d'écriture prend le verrou d'écriture dès le début) : les vérifications de quota et d'envoi unique de deux requêtes simultanées sont sérialisées. Les changements de statut sont des « compare-and-set ».

---

## 7. Tests automatisés

```bash
python manage.py test
```

97 tests couvrent notamment : comptage des segments (160/70, accents, emoji, limite de 6), opérateurs (bornes des plages), validation, envoi unique (rejeu et conflit), quota (dépassement, restitution à l'annulation, remise à zéro à minuit au Bénin), isolation entre applications, annulation selon le statut, statuts définitifs, priorité des OTP, plage horaire (soumission, programmation, nouvelle tentative qui passe 21 h), signature des comptes rendus (fausse, absente, d'un autre opérateur), comptes rendus en double ou d'une ancienne tentative, nouvelles tentatives avec délais croissants, **les 8 scénarios du simulateur de bout en bout**, campagnes (quota par destinataire, doublons, limites 1–500), et l'absence du texte des OTP dans les réponses et les journaux.

---

## 8. Configuration

Variables d'environnement (toutes facultatives) :

| Variable | Défaut | Rôle |
|---|---|---|
| `ETATCIVIL_API_KEY`, `COURRIER_API_KEY` | `etatcivil-test-key`, `courrier-test-key` | Clés d'API des applications. |
| `CELTIIS_SECRET`, `MTN_SECRET`, `MOOV_SECRET` | `celtiis-secret`, `mtn-secret`, `moov-secret` | Secrets de signature des comptes rendus. |
| `GATEWAY_BASE_URL` | `http://127.0.0.1:8000` | URL de l'API, utilisée par le simulateur pour renvoyer ses comptes rendus. |
| `GATEWAY_FAKE_NOW` | (vide) | Heure simulée de départ, heure du Bénin (ex. `2026-11-04T23:30`). |
| `RETRY_DELAYS_SECONDS` | `2,4,8` | Délais avant les nouvelles tentatives 1, 2, 3. |
| `SIMULATOR_REPORT_DELAY_SECONDS` | `1` | Délai entre transmission et compte rendu simulé. |
| `WORKER_INTERVAL_SECONDS` | `1` | Intervalle entre deux passages du worker. |
| `DATABASE_PATH` | `db.sqlite3` | Fichier SQLite. |
| `DJANGO_DEBUG` | `1` | `0` en production. |

---

## 9. État d'avancement

### Ce qui fonctionne (socle obligatoire complet)

- [x] Soumettre un SMS : validation du numéro et de l'opérateur, segments, quota, envoi unique, envoi programmé.
- [x] Soumettre une campagne : 500 destinataires au plus, résultat par destinataire, quota destinataire par destinataire.
- [x] Simuler les opérateurs : accusé de réception ou indisponibilité, puis compte rendu signé ; tous les cas demandés sont reproductibles.
- [x] Transmettre chaque SMS à l'opérateur du destinataire et tenir compte de son compte rendu (signature vérifiée, statuts définitifs respectés, jusqu'à 3 nouvelles tentatives espacées).
- [x] Consulter ses SMS (avec historique) et ses campagnes, sa consommation du jour ; annuler un SMS non transmis.
- [x] Plage horaire des INFO et priorité des OTP.
- [x] Confidentialité des OTP, isolation entre applications, erreurs claires avec le bon code HTTP.

### Bonus

- [x] **Tests automatisés des cas critiques** (97 tests).
- [~] **Quota et envoi unique sous requêtes simultanées** : traité dans la conception (SQLite `IMMEDIATE` qui sérialise les écritures, contrainte d'unicité `(application, référence)` avec reprise en rejeu sur conflit, transitions conditionnelles), mais **pas démontré par un test de charge concurrente**.
- [ ] Rappel signé vers l'application cliente : non fait, faute de temps.
- [ ] SMS dont le compte rendu n'arrive jamais : non fait ; le scénario du simulateur existe déjà (numéro finissant par `7`), il manquerait un délai d'expiration dans le worker qui fasse passer ces SMS en échec (ou les relance).
- [ ] Tableau de supervision : non fait, faute de temps.

### Limites connues

- **Comptes rendus du simulateur** : ils sont programmés par des minuteurs dans le processus du worker. Si le worker est arrêté juste après une transmission, le compte rendu de ce SMS est perdu et le SMS reste `TRANSMIS` (c'est précisément le cas « compte rendu jamais reçu » du bonus).
- **SQLite** convient à l'épreuve et à un seul worker ; en production on passerait à PostgreSQL (`SELECT … FOR UPDATE SKIP LOCKED` pour plusieurs workers) et à des délais de nouvelle tentative en minutes.
- **Le texte des OTP est stocké en clair en base** (il faut bien le transmettre à l'opérateur) ; il n'apparaît ni dans les journaux ni dans les réponses. En production : chiffrement sera fait.
