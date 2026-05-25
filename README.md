# NEXUS SMC BOT — Deriv WebSocket (Cloud)

## Déploiement sur Railway (GRATUIT)

### Étape 1 — Créer un compte GitHub
1. Va sur github.com
2. Crée un compte gratuit

### Étape 2 — Créer un dépôt GitHub
1. Clique "New repository"
2. Nom : "nexus-smc-bot"
3. Clique "Create repository"
4. Upload les fichiers : bot.py, requirements.txt, Procfile

### Étape 3 — Déployer sur Railway
1. Va sur railway.app
2. Connecte-toi avec GitHub
3. Clique "New Project" → "Deploy from GitHub repo"
4. Sélectionne "nexus-smc-bot"
5. Va dans "Variables" → ajoute :
   - Nom : DERIV_TOKEN
   - Valeur : TON_TOKEN_API_DERIV
6. Clique "Deploy"

### Le bot tourne maintenant 24h/24 sur le cloud !

## Surveiller le bot
- Va sur Railway → ton projet → "Logs"
- Tu verras tous les trades en temps réel

## Arrêter le bot
- Railway → ton projet → "Settings" → "Remove"
