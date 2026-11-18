cd strategy-A1/live

docker ps -aq | xargs -r docker rm -f
docker volume ls -q | xargs -r docker volume rm -f
docker-compose -f docker-compose.yml down --volumes --rmi all --remove-orphans
docker system prune -a --volumes -f
docker rm -f $(docker ps -aq) 2>/dev/null; docker system prune -a --volumes -f; docker builder prune -af

cd ../..

rm -rf strategy-A1
rm -rf nginx
