git pull origin main

cd live
docker-compose down && docker-compose build --no-cache && docker-compose up -d --force-recreate