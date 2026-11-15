git pull origin main

cd live
docker-compose -p blacker-bot1 down && docker-compose -p blacker-bot1 build --no-cache && docker-compose -p blacker-bot1 up -d --force-recreate
