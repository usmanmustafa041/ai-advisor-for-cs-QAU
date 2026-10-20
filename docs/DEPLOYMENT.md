# Department server par deployment (step by step)

Ye guide department ke Windows server (`10.10.10.112`, Remote Desktop se) par
QAU CS Advisor chalane ke liye hai. Sab commands **PowerShell** mein chalani hain,
project folder ke andar.

Poora system Docker Compose se 4 containers mein chalta hai:

| Container | Kaam |
|---|---|
| `postgres` | Database (users, courses, fees, timetable, chat history) |
| `backend` | Portal API — login, courses, fees, timetable, admin |
| `advisor` | Chatbot engine — sawalon ke jawab |
| `web` | Nginx — website dikhata hai aur `/api`, `/advisor` ko andar bhejta hai |

Students sirf `web` (port 80) se baat karte hain. Database aur APIs bahar nahi khulte.

---

## 0. Server check karein (5 minute)

Remote Desktop se login karke PowerShell kholein:

```powershell
(Get-ComputerInfo).OsName          # Windows 10/11 ya Windows Server?
docker --version                   # Docker pehle se hai?
netstat -ano | findstr ":80 "      # Port 80 pehle se kisi (IIS waghera) ke paas to nahi?
Test-NetConnection google.com -Port 443   # Internet hai?
```

- **Windows 10/11:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) install karein
  (WSL2 wala option), restart karein, Docker Desktop khol kar "Engine running" ka intezar karein.
- **Windows Server:** Docker Desktop officially support nahi karta. Pehle IT/teacher se poochein ke
  Docker ya WSL2 already hai ya nahi. Na ho to ruk jayein. Is ka setup alag hai.
- **Port 80 busy ho** to neeche `.env` mein `WEB_PORT=8080` rakhein. Phir address
  `http://10.10.10.112:8080` hoga.
- Server ke paas **8 GB+ RAM** aur **~15 GB khali disk** honi chahiye. Docker images mein
  ML models hain.

## 1. Project server par lana

**Tareeqa A: GitHub se (agar server par internet aur git hai):**

```powershell
git clone https://github.com/usmanmustafa041/ai-advisor-for-cs-QAU.git
cd ai-advisor-for-cs-QAU
```

(Pehle apne laptop se `deploy-prep` branch push/merge karna zaroori hai.)

**Tareeqa B: USB/zip se:** laptop se poora `ai-advisor-for-cs-QAU` folder copy karein, lekin
`frontend/node_modules`, `web/node_modules` aur apni laptop wali `.env` **na** copy karein.

## 2. `.env` banana (server ke liye naye secrets)

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # -> POSTGRES_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(48))"   # -> AUTH_SECRET
```

Python na ho to laptop par ye values bana kar laayein. `notepad .env` kholein aur bharein:

- `POSTGRES_PASSWORD=` → pehli random value
- `AUTH_SECRET=` → doosri random value
- `GEMINI_API_KEY=` → apni Gemini key (is ke baghair jawab sirf passages honge, ya Ollama chahiye)
- `WEB_PORT=80` (ya 8080, step 0 dekhein)

Laptop wale secrets server par dobara use na karein. `.env` kisi ko share na karein.

## 3. Start karna

```powershell
docker compose up --build -d
docker compose ps
```

Pehli dafa build mein **15–40 minute** lag sakte hain (Python packages + models download).
Tamaam containers `healthy` ho jayein to aage chalein. Masla ho to:

```powershell
docker compose logs backend --tail 50
docker compose logs advisor --tail 50
```

## 4. Admin password badalna (ZAROORI — pehle 5 minute mein)

Database pehli dafa `admin@cs.qau.edu.pk` ek **public password** ke saath banata hai. Foran badlein:

```powershell
docker compose exec backend python scripts/set_admin_password.py
```

Kam az kam 12 characters ka password dein. Ye password teacher ko alag se (chat mein nahi) dein.

## 5. Chatbot ka knowledge index

Naye server par chatbot engine ka index khali hota hai. Do tareeqe hain:

**Tareeqa A: server par internet ho:** website crawl karke naya index banaye:

```powershell
docker compose exec advisor python -m advisor.cli refresh
docker compose restart advisor
```

**Tareeqa B: laptop wala tayyar index copy karein** (internet ki zaroorat nahi). Ek tayyar
copy `deploy-bundle/engine-data.tgz` mein pehle se bani hui hai (git mein nahi jati). Nayi
banani ho to laptop par (jab stack chal raha ho):

```powershell
docker compose exec advisor tar czf /tmp/engine-data.tgz -C /data .
docker compose cp advisor:/tmp/engine-data.tgz .\deploy-bundle\engine-data.tgz
```

`deploy-bundle\engine-data.tgz` USB se server ke project folder ke `deploy-bundle\` mein laayein,
phir server par:

```powershell
docker compose cp .\deploy-bundle\engine-data.tgz advisor:/tmp/engine-data.tgz
docker compose exec advisor tar xzf /tmp/engine-data.tgz -C /data
docker compose restart advisor
```

## 6. Network par kholna

Windows Firewall mein port kholein (Administrator PowerShell):

```powershell
New-NetFirewallRule -DisplayName "QAU Advisor web" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
```

(`WEB_PORT` 8080 hai to `-LocalPort 8080`.)

## 7. Check karna

Server par:

```powershell
curl.exe http://localhost/nginx-health          # ok
curl.exe http://localhost/api/v1/health
curl.exe http://localhost/advisor/api/v1/health
```

Phir department ke kisi **doosre** computer ke browser mein `http://10.10.10.112` kholein:

1. Chat mein ek sawal poochein (masalan "BSCS mein Data Structures ka prerequisite kya hai?")
2. Admin se login karein → Dashboard aur *Chatbot Engine* page khulein.

## 8. Roz-marrah ke kaam

| Kaam | Command |
|---|---|
| Status dekhna | `docker compose ps` |
| Logs | `docker compose logs -f advisor` |
| Band karna (data mehfooz rehta hai) | `docker compose down` |
| Dobara chalana | `docker compose up -d` |
| Code update ke baad | `git pull` phir `docker compose up --build -d` |
| Website data refresh | `docker compose exec advisor python -m advisor.cli refresh` |

Server restart hone par containers khud chal jate hain (`restart: unless-stopped`), basharte
Docker Desktop "Start when you sign in" par set ho ya Docker service automatic ho.

**Kabhi na chalayein:** `docker compose down -v`. `-v` database aur chatbot data **delete** kar deta hai.

## Note: sirf department ke andar

`10.10.10.112` private address hai. Website sirf department ke network se khulegi. Bahar
(internet) se kholne ke liye university IT se public address/port forwarding aur HTTPS
certificate chahiye. Wo alag kaam hai.
