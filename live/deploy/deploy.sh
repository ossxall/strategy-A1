sudo apt update
sudo apt install git -y

git clone https://github.com/ossxall/strategy-A1.git
cd strategy-A1

git checkout main

sudo apt install docker-compose -y
docker-compose --version


sudo apt-get update
sudo apt-get install apache2-utils
sudo htpasswd -cB nginx/.htpasswd operador
sudo chmod 644 nginx/.htpasswd
docker-compose -p blacker-bot1 restart nginx