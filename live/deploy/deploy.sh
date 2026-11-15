sudo apt update
sudo apt install git -y

git clone https://github.com/ossxall/strategy-A1.git
cd strategy-A1

git checkout main

sudo apt install docker-compose -y
docker-compose --version
