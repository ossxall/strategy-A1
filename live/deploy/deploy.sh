sudo apt update
sudo apt install git -y
sudo apt install docker-compose -y

git clone https://github.com/ossxall/strategy-A1.git
cd strategy-A1

git checkout main

export APP_ENV=prod
echo $APP_ENV

cd ..