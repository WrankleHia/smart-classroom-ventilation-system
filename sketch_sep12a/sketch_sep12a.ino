#include <WiFi.h>
#include <FS.h>
#include "time.h"
#include <WebServer.h>
#include <DNSServer.h>
#include <SPI.h>
#include <HardwareSerial.h>
#include <TFT_eSPI.h>
#include <HTTPClient.h>
#include <DHT.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <SD.h>

// 使用HSPI总线来避免与TFT屏幕的VSPI总线冲突
SPIClass hspi(HSPI);
const char* DEVICE_SERIAL_NUMBER = "CH202509240003"; // 这里设备序列号可以随便改

// --- 用户配置区 ---
char ssid[33];
char password[65];
char server_ip[16];
char username[33];
char user_password[65];
char api_token[65] = "";
String pending_command = "";
String serverName;

// --- Web服务器和网络对象 ---
WebServer webServer(80);
DNSServer dnsServer;
Preferences preferences;
bool configurationMode = false;
const char* ntpServer1 = "ntp.aliyun.com";
const char* ntpServer2 = "pool.ntp.org";
const char* ntpServer3 = "time.windows.com";
const long gmtOffset_sec = 8 * 3600;
const int daylightOffset_sec = 0;

// --- 引脚定义 ---
#define RX2_PIN 16
#define TX2_PIN 17
#define RED_LED 22
#define DHTPIN 5
#define JOYSTICK_SW_PIN 4

// --- 对象实例化 ---
HardwareSerial co2Serial(2);
DHT dht(DHTPIN, DHT11);
TFT_eSPI tft = TFT_eSPI();
TFT_eSprite spr = TFT_eSprite(&tft);

// --- 传感器和状态数据 ---
int currentCO2 = 0;
float currentTemperature = 0.0;
int currentPeopleCount = -1;
String currentFilterStatus = "Unknown";
int maxCO2 = 0;
int minCO2 = 9999;
unsigned long readingCount = 0;
unsigned long buttonPressStartTime = 0;
enum LinkState { LINK_OK, LINK_FAILED, LINK_PENDING };
LinkState currentLinkState = LINK_PENDING;
enum DisplayState { DISPLAY_CO2, DISPLAY_TEMP, DISPLAY_PEOPLE, DISPLAY_VENTILATION };
DisplayState currentDisplayState = DISPLAY_CO2;
unsigned long lastScreenSwitchTime = 0;
const unsigned long SCREEN_SWITCH_INTERVAL = 10000;
unsigned long lastServerUpdateTime = 0;
const unsigned long SERVER_UPDATE_INTERVAL = 1000;
unsigned long lastDataFetchTime = 0;
const unsigned long DATA_FETCH_INTERVAL = 1000;
const unsigned long WARMUP_TIME = 60000;
unsigned long startTime;
bool isWarmedUp = false;
int co2Threshold = 1000; // 默认阈值, 将由服务器更新

// --- CSV日志记录变量 ---
String currentCsvFilename = "";
unsigned long lastCsvWriteTime = 0;
const unsigned long CSV_WRITE_INTERVAL = 30000;

// --- 重连与心跳逻辑变量 ---
unsigned long lastAuthAttemptTime = 0;
const unsigned long AUTH_RETRY_INTERVAL = 30000; 
unsigned long lastSuccessfulLinkTime = 0;

bool syncTime();


void setup() {
    Serial.begin(115200);
    pinMode(JOYSTICK_SW_PIN, INPUT_PULLUP);
    
    tft.init();
    tft.setRotation(0);
    spr.setColorDepth(8);
    spr.createSprite(240, 240);
    
    hspi.begin(14, 13, 12, 26);
    if (!SD.begin(26, hspi)) {
        Serial.println("SD init failed!");
    } else {
        Serial.println("SD init success!");
    }

    if (!loadCredentials()) {
        configurationMode = true;
        setupSoftAP();
        showConfigurationScreen();
    } else {
        configurationMode = false;
        dht.begin();
        
        connectToWiFi();

        if (WiFi.status() == WL_CONNECTED) {
            if (syncTime()) {

                createNewCsvFile();
            }

            registerDeviceWithServer();
            // 显示 Registering
            authenticateAndGetToken(); // 显示 Authenticating
        }
        
        showStartupScreen();
        co2Serial.begin(9600, SERIAL_8N1, RX2_PIN, TX2_PIN);
        startTime = millis();
        Serial.println("System initialized. Starting warmup...");
    }
}

void loop() {
    // 如果处于配置模式，则处理Web服务器请求
    if (configurationMode) {
        dnsServer.processNextRequest();
        webServer.handleClient();
        return;
    }

    // 如果传感器未预热完毕，则处理预热流程
    if (!isWarmedUp) {
        handleWarmup();
    } else {
        // 检查是否需要恢复出厂设置
        handleFactoryResetCheck();
        // 读取传感器数据
        readCO2Data();
        readDHTData();

        // 自动切换显示屏幕
        if (millis() - lastScreenSwitchTime > SCREEN_SWITCH_INTERVAL) {
            lastScreenSwitchTime = millis();
            if (buttonPressStartTime == 0) {
               currentDisplayState = (DisplayState)((currentDisplayState + 1) % 4);
            }
        }
        
        // 确保无论是离线还是在线，都定期向CSV文件写入数据
        if (millis() - lastCsvWriteTime > CSV_WRITE_INTERVAL) {
            lastCsvWriteTime = millis();
            writeCsvLogEntry();
        }
        
        // 检查Wi-Fi连接和服务器认证状态
        if (WiFi.status() == WL_CONNECTED && strlen(api_token) > 0) {
            
            // 检查与服务器的链接是否超时
            const unsigned long LINK_TIMEOUT = 5000;
            if (currentLinkState == LINK_OK && (millis() - lastSuccessfulLinkTime > LINK_TIMEOUT)) {
                Serial.println("Link timeout! No successful server response. Marking link as FAILED.");
                currentLinkState = LINK_FAILED;
            }

            // 定期向服务器发送数据
            if (millis() - lastServerUpdateTime > SERVER_UPDATE_INTERVAL) {
                lastServerUpdateTime = millis();
                sendDataToServer();
            }
            // 定期从服务器获取数据
            if (millis() - lastDataFetchTime > DATA_FETCH_INTERVAL) {
                lastDataFetchTime = millis();
                fetchDataFromServer();
            }
            
            // 处理从服务器接收到的待处理命令
            if (pending_command != "" && pending_command != "NONE") {
                if (pending_command == "LIST_FILES") listAndUploadFiles();
                else if (pending_command.startsWith("READ_FILE:")) readAndUploadSdCardFile(pending_command.substring(10));
                else if (pending_command.startsWith("DELETE_FILE:")) deleteSdCardFile(pending_command.substring(12));
                pending_command = "";
            }

        } else {

            currentPeopleCount = -1;
            currentLinkState = LINK_FAILED;

            // 后台重连逻辑
            if (WiFi.status() == WL_CONNECTED && strlen(api_token) == 0) {
                if (millis() - lastAuthAttemptTime > AUTH_RETRY_INTERVAL) {
                    lastAuthAttemptTime = millis();
                    // 静默注册和认证
                    if (registerDeviceSilent() && authenticateAndGetTokenSilent()) {

                        Serial.println("Re-authenticated with server successfully.");
                    }
                }
            }
        }
        
        // 更新TFT屏幕显示
        updateDisplay();
    }
    
    delay(100);
}

bool syncTime() {
    spr.fillSprite(TFT_BLACK);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    spr.setTextSize(2);
    spr.setCursor(30, 100);
    spr.print("Syncing Time...");
    spr.pushSprite(0, 0);

    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer1, ntpServer2, ntpServer3);
    
    struct tm timeinfo;
    unsigned long startAttemptTime = millis();
    bool time_synced = false;

    // 等待最多15秒
    while (millis() - startAttemptTime < 15000) {
        if (getLocalTime(&timeinfo)) {
            time_synced = true;
            break;
        }
        delay(500);
    }
    
    spr.fillSprite(TFT_BLACK);
    if (time_synced) {
        spr.setCursor(20, 100); spr.print("Time Synchronized!");
        Serial.println("Time synchronized successfully.");
    } else {
        spr.setCursor(30, 100); spr.print("Time Sync Failed!");
        Serial.println("Time synchronization failed.");
    }
    spr.pushSprite(0, 0);
    delay(2000);
    return time_synced;
}

void showDateTime() {
    struct tm timeinfo;
    
    if (getLocalTime(&timeinfo)) {
        char timeString[9];
        sprintf(timeString, "%02d:%02d:%02d", timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
        spr.setTextSize(1);
        spr.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
        spr.setCursor(240 - spr.textWidth(timeString) - 5, 230);
        spr.print(timeString);
    }
}

void createNewCsvFile() {
    struct tm timeinfo;
    if (!getLocalTime(&timeinfo)) {
        Serial.println("Time not available, cannot create CSV file.");
        return;
    }
    
    char filenameBuffer[40];
    sprintf(filenameBuffer, "/%d-%02d-%02d-%02d%02d%02d.csv", 
            timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday, 
            timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);

    currentCsvFilename = String(filenameBuffer);
    Serial.printf("Creating new log file: %s\n", currentCsvFilename.c_str());
    
    File file = SD.open(currentCsvFilename, FILE_WRITE);
    if (file) {
        file.println("Timestamp,PeopleCount,CO2,Temperature,FilterStatus,VentilationStatus,LinkStatus");
        file.close();
        Serial.println("CSV file created and header written.");
    } else {
        Serial.println("Failed to create CSV file.");
        currentCsvFilename = "";
    }
}

void writeCsvLogEntry() {
    if (currentCsvFilename == "") return;
    
    File file = SD.open(currentCsvFilename, FILE_APPEND);
    if (!file) return;

    char timeBuffer[20];
    struct tm timeinfo;

    if (getLocalTime(&timeinfo)) {
        sprintf(timeBuffer, "%d-%02d-%02d %02d:%02d:%02d", 
                timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
                timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
    } else {
        strcpy(timeBuffer, "Time_Sync_Lost");
    }

    String dataLine = String(timeBuffer) + "," + 
                      (currentPeopleCount == -1 ? "N/A" : String(currentPeopleCount)) + "," + 
                      String(currentCO2) + "," + 
                      String(currentTemperature, 1) + "," + 
                      currentFilterStatus + "," + 
                      getVentilationStatusString() + "," + 
                      getLinkStatusString();
                      
    file.println(dataLine);
    file.close();
}

void handleWarmup() {
    unsigned long lastScreenUpdateTime = 0;
    while (millis() - startTime < WARMUP_TIME) {
        if (digitalRead(JOYSTICK_SW_PIN) == LOW) {
            isWarmedUp = true;
            showReadyScreen();
            delay(1000);
            return;
        }
        if (millis() - lastScreenUpdateTime >= 1000) {
            lastScreenUpdateTime = millis();
            int remaining = (WARMUP_TIME - (millis() - startTime)) / 1000;
            if (remaining < 0) remaining = 0;
            showWarmupScreen(remaining);
        }
        delay(20);
    }
    isWarmedUp = true;
    showReadyScreen();
    delay(1000);
}

void connectToWiFi() {
    spr.fillSprite(TFT_BLACK);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    spr.setTextSize(2);
    spr.setCursor(20, 100);
    spr.print("Connecting WiFi...");
    spr.pushSprite(0, 0);
    WiFi.begin(ssid, password);
    int connect_timeout = 20;
    while (WiFi.status() != WL_CONNECTED && connect_timeout > 0) {
        delay(500);
        Serial.print(".");
        connect_timeout--;
    }
    spr.fillSprite(TFT_BLACK);
    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\nWiFi connected!");
        spr.setCursor(30, 100);
        spr.print("WiFi Connected!");
    } else {
        Serial.println("\nFailed to connect to WiFi.");
        spr.setCursor(30, 100);
        spr.print("WiFi Failed!");
    }
    spr.pushSprite(0, 0);
    delay(2000);
}

void registerDeviceWithServer() {
    if (WiFi.status() != WL_CONNECTED) return;
    spr.fillSprite(TFT_BLACK);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    spr.setTextSize(2);
    spr.setCursor(20, 100);
    spr.print("Registering device...");
    spr.pushSprite(0, 0);
    HTTPClient http;
    http.setTimeout(3000);
    String serverPath = "http://" + String(server_ip) + ":5933/devices/register";
    http.begin(serverPath);
    http.addHeader("Content-Type", "application/x-www-form-urlencoded");
    String postData = "user=" + String(username) + "&pass=" + String(user_password) + "&serial=" + String(DEVICE_SERIAL_NUMBER);
    int httpCode = http.POST(postData);
    spr.fillSprite(TFT_BLACK);
    if (httpCode == HTTP_CODE_OK) {
        spr.setCursor(10, 100);
        spr.print("Device Registered!");
    } else {
        spr.setCursor(10, 100);
        spr.printf("Reg. Failed: %d", httpCode);
    }
    spr.pushSprite(0, 0);
    http.end();
    delay(2000);
}

bool authenticateAndGetToken() {
    if (WiFi.status() != WL_CONNECTED) return false;
    spr.fillSprite(TFT_BLACK);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    spr.setTextSize(2);
    spr.setCursor(20, 100);
    spr.print("Authenticating...");
    spr.pushSprite(0, 0);
    HTTPClient http;
    http.setTimeout(3000);
    String serverPath = "http://" + String(server_ip) + ":5933/api/login";
    http.begin(serverPath);
    http.addHeader("Content-Type", "application/json");
    StaticJsonDocument<200> doc;
    doc["user"] = username;
    doc["pass"] = user_password;
    doc["serial"] = DEVICE_SERIAL_NUMBER;
    String requestBody;
    serializeJson(doc, requestBody);
    int httpCode = http.POST(requestBody);
    spr.fillSprite(TFT_BLACK);
    if (httpCode == HTTP_CODE_OK) {
        String payload = http.getString();
        DynamicJsonDocument responseDoc(256);
        deserializeJson(responseDoc, payload);
        const char* token = responseDoc["token"];
        if (token) {
            strncpy(api_token, token, sizeof(api_token) - 1);
            api_token[sizeof(api_token) - 1] = '\0';
            spr.setCursor(10, 100);
            spr.print("Auth Success!");
            spr.pushSprite(0, 0);
            http.end();
            delay(2000);
            return true;
        }
    }
    spr.setCursor(10, 100);
    spr.printf("Auth Failed: %d", httpCode);
    spr.pushSprite(0, 0);
    http.end();
    delay(2000);
    return false;
}

void fetchDataFromServer() {
    if (WiFi.status() != WL_CONNECTED || strlen(api_token) == 0) {
        currentLinkState = LINK_FAILED;
        return;
    }
    HTTPClient http;
    http.setTimeout(1500);
    http.begin(serverName); 
    http.addHeader("Authorization", "Bearer " + String(api_token));
    int httpCode = http.GET();
    if (httpCode == HTTP_CODE_OK) {
        DynamicJsonDocument doc(1024);
        deserializeJson(doc, http.getString());
        if (doc.containsKey("people_count")) {
            currentPeopleCount = doc["people_count"];
            currentLinkState = LINK_OK;
            lastSuccessfulLinkTime = millis();
        } else {
            currentLinkState = LINK_FAILED;
        }
        if (doc.containsKey("filter_status")) currentFilterStatus = doc["filter_status"].as<String>();
        if (doc.containsKey("command")) pending_command = doc["command"].as<String>();
        if (doc.containsKey("co2_threshold")) {
            co2Threshold = doc["co2_threshold"];
        }
    } else {
        currentLinkState = LINK_FAILED;
    }
    http.end();
}

void sendDataToServer() {
    if (WiFi.status() != WL_CONNECTED || strlen(api_token) == 0) return;
    HTTPClient http;
    http.setTimeout(1500);
    uint64_t sdTotal = SD.totalBytes();
    uint64_t sdUsed = SD.usedBytes();
    String serverPath = serverName + "?co2=" + String(currentCO2) + "&temp=" + String(currentTemperature, 1) + "&ventilation=" + getVentilationStatusString() + "&sd_total=" + String(sdTotal) + "&sd_used=" + String(sdUsed);
    http.begin(serverPath.c_str());
    http.addHeader("Authorization", "Bearer " + String(api_token));
    if (http.GET() < 0) {
        currentLinkState = LINK_FAILED;
    }
    http.end();
}

bool registerDeviceSilent() {
    if (WiFi.status() != WL_CONNECTED) return false;
    HTTPClient http;
    http.setTimeout(2000);
    http.begin("http://" + String(server_ip) + ":5933/devices/register");
    http.addHeader("Content-Type", "application/x-www-form-urlencoded");
    String postData = "user=" + String(username) + "&pass=" + String(user_password) + "&serial=" + String(DEVICE_SERIAL_NUMBER);
    int httpCode = http.POST(postData);
    http.end();
    return (httpCode == HTTP_CODE_OK);
}

bool authenticateAndGetTokenSilent() {
    if (WiFi.status() != WL_CONNECTED) return false;
    HTTPClient http;
    http.setTimeout(2000);
    http.begin("http://" + String(server_ip) + ":5933/api/login");
    http.addHeader("Content-Type", "application/json");
    StaticJsonDocument<200> doc;
    doc["user"] = username;
    doc["pass"] = user_password;
    doc["serial"] = DEVICE_SERIAL_NUMBER;
    String requestBody;
    serializeJson(doc, requestBody);
    int httpCode = http.POST(requestBody);
    if (httpCode == HTTP_CODE_OK) {
        DynamicJsonDocument responseDoc(256);
        deserializeJson(responseDoc, http.getString());
        const char* token = responseDoc["token"];
        if (token) {
            strncpy(api_token, token, sizeof(api_token) - 1);
            api_token[sizeof(api_token) - 1] = '\0';
            lastSuccessfulLinkTime = millis();
            http.end();
            return true;
        }
    }
    http.end();
    return false;
}

void listAndUploadFiles() {
    File root = SD.open("/");
    DynamicJsonDocument doc(1024);
    doc["type"] = "list";
    JsonArray files = doc.createNestedArray("files");
    if (root) {
        File file = root.openNextFile();
        while(file){
            if(!file.isDirectory()){
                String filename = file.name();
                files.add(filename.startsWith("/") ? filename.substring(1) : filename);
            }
            file = root.openNextFile();
        }
    }
    String jsonBuffer;
    serializeJson(doc, jsonBuffer);
    postDataToServer(jsonBuffer);
}

void readAndUploadSdCardFile(String filepath) {
    String fullpath = "/" + filepath;
    File file = SD.open(fullpath, FILE_READ);
    DynamicJsonDocument doc(2048);
    doc["type"] = "content";
    if (!file) {
        doc["content"] = "Error: Failed to open file on device.";
    } else {
        String fileContent = "";
        while (file.available()) {
            fileContent += (char)file.read();
        }
        file.close();
        doc["content"] = fileContent;
    }
    String jsonBuffer;
    serializeJson(doc, jsonBuffer);
    postDataToServer(jsonBuffer);
}

void deleteSdCardFile(String filepath) {
    SD.remove("/" + filepath);
    listAndUploadFiles();
}

String getLinkStatusString() {
    switch(currentLinkState) {
        case LINK_OK: return "OK";
        case LINK_FAILED: return "Failed";
        case LINK_PENDING: return "Pending";
        default: return "Unknown";
    }
}

String getVentilationStatusString() {
    return (currentCO2 > co2Threshold) ? "ON" : "OFF";
}

void postDataToServer(String jsonData) {
    HTTPClient http;
    http.setTimeout(1500);
    http.begin("http://" + String(server_ip) + ":5933/devices/upload_sd_data");
    http.addHeader("Authorization", "Bearer " + String(api_token));
    http.addHeader("Content-Type", "application/json");
    http.POST(jsonData);
    http.end();
}

void readDHTData() {
    float t = dht.readTemperature();
    if (!isnan(t)) { currentTemperature = t; }
}

void readCO2Data() {
    if (co2Serial.find(0x2C)) {
        byte data[8];
        if (co2Serial.readBytes(data, 8) == 8) {
            byte checksum = 0x2C;
            for (int i = 0; i < 7; i++) { checksum += data[i]; }
            if (checksum == data[7]) {
                int co2_value = data[5] * 256 + data[6];
                if (co2_value >= 250 && co2_value < 10000) {
                    currentCO2 = co2_value;
                    if(readingCount++ == 0) maxCO2 = minCO2 = currentCO2;
                    else {
                        if (currentCO2 > maxCO2) maxCO2 = currentCO2;
                        if (currentCO2 < minCO2) minCO2 = currentCO2;
                    }
                }
            }
        }
    }
}

void updateDisplay() {
    spr.fillSprite(TFT_BLACK);
    switch (currentDisplayState) {
    case DISPLAY_CO2: showCO2MonitorMenu(); break;
    case DISPLAY_TEMP: showTemperatureMenu(); break;
    case DISPLAY_PEOPLE: showClassroomCountMenu(); break;
    case DISPLAY_VENTILATION: showVentilationMenu(); break;
    }
    if (buttonPressStartTime != 0) {
        unsigned long heldDuration = millis() - buttonPressStartTime;
        int remainingSeconds = 10 - (heldDuration / 1000);
        if (remainingSeconds < 0) remainingSeconds = 0;
        spr.fillRect(30, 80, 180, 80, TFT_DARKGREY);
        spr.drawRect(29, 79, 182, 82, TFT_WHITE);
        spr.setTextColor(TFT_RED, TFT_DARKGREY); spr.setTextSize(2);
        spr.setCursor(45, 95); spr.print("Clearing data in:");
        spr.setTextSize(4); spr.setCursor(105, 120); spr.print(remainingSeconds);
    }
    showDateTime(); showMenuIndicator(); showPageIndicator(); showLinkStatus();
    spr.pushSprite(0, 0);
}

void handleFactoryResetCheck() {
    if (digitalRead(JOYSTICK_SW_PIN) == LOW) {
        if (buttonPressStartTime == 0) buttonPressStartTime = millis();
        else if (millis() - buttonPressStartTime >= 10000) {
            preferences.begin("net-config", false); preferences.clear(); preferences.end();
            spr.fillSprite(TFT_BLACK); spr.setTextColor(TFT_GREEN); spr.setTextSize(2);
            spr.setCursor(30, 100); spr.println("Settings Cleared!");
            spr.setCursor(60, 140); spr.println("Rebooting...");
            spr.pushSprite(0, 0); delay(3000); ESP.restart();
        }
    } else {
        buttonPressStartTime = 0;
    }
}

void showLinkStatus() {
    uint16_t linkColor;
    switch (currentLinkState) {
    case LINK_OK: linkColor = TFT_GREEN; break;
    case LINK_FAILED: linkColor = TFT_RED; break;
    case LINK_PENDING: default: linkColor = TFT_YELLOW; break;
    }
    spr.setTextSize(2); spr.setTextColor(linkColor); spr.setCursor(5, 9); spr.print("LINK");
}

void showPageIndicator() {
    int circleY = 215, radius = 4, spacing = 20;
    int startX = (240 - (4 * (2 * radius) + 3 * spacing)) / 2 + radius;
    for (int i = 0; i < 4; i++) {
        if (i == currentDisplayState) spr.fillCircle(startX + i * (2 * radius + spacing), circleY, radius, TFT_WHITE);
        else spr.drawCircle(startX + i * (2 * radius + spacing), circleY, radius, TFT_DARKGREY);
    }
}

void showStartupScreen() {
    spr.fillSprite(TFT_BLACK); spr.setTextColor(TFT_CYAN, TFT_BLACK);
    spr.setTextSize(3); spr.setCursor(20, 70); spr.println("Sensor Hub");
    spr.pushSprite(0, 0); delay(1500);
}

void showWarmupScreen(int sec) {
    spr.fillSprite(TFT_BLACK); spr.setTextColor(TFT_ORANGE, TFT_BLACK);
    spr.setTextSize(3); spr.setCursor(40, 50); spr.println("WARMING UP");
    spr.setTextColor(TFT_YELLOW, TFT_BLACK); spr.setTextSize(6);
    spr.setCursor( (sec < 10) ? 110 : 90, 120); spr.println(sec);
    int progress = (int)(((float)(WARMUP_TIME / 1000 - sec) / (WARMUP_TIME / 1000)) * 200.0);
    spr.drawRect(20, 200, 200, 16, TFT_WHITE); spr.fillRect(22, 202, progress, 12, TFT_GREEN);
    spr.setTextColor(TFT_WHITE, TFT_BLACK); spr.setTextSize(1);
    spr.setCursor(65, 228); spr.println("Press button to skip"); spr.pushSprite(0, 0);
}

void showClassroomCountMenu() {
    spr.fillRect(0, 0, 240, 32, TFT_NAVY);
    spr.setTextColor(TFT_WHITE, TFT_NAVY);
    spr.setTextSize(2);
    spr.setCursor(65, 9);
    spr.println("People Count");
    if (currentPeopleCount == -1) {
        spr.setTextColor(TFT_RED, TFT_BLACK);
        spr.setTextSize(6);
        spr.setCursor(65, 80);
        spr.print("N/A");
    } else {
        spr.setTextColor(TFT_WHITE, TFT_BLACK);
        spr.setTextSize(8);
        spr.setCursor( (currentPeopleCount < 10) ? 100 : 70, 70);
        spr.print(currentPeopleCount);
    }
    spr.setTextSize(2);
    spr.setCursor(95, 140);
    spr.print("People");
}

void showTemperatureMenu() {
    spr.fillRect(0, 0, 240, 32, TFT_BLUE);
    spr.setTextColor(TFT_WHITE, TFT_BLUE);
    spr.setTextSize(2);
    spr.setCursor(65, 9);
    spr.println("Temperature");
    spr.setTextColor(TFT_ORANGE, TFT_BLACK);
    String tempStr = String(currentTemperature, 1);
    spr.setTextSize(6);
    int startX = (240 - spr.textWidth(tempStr + " C")) / 2;
    spr.setCursor(startX, 80);
    spr.print(tempStr);
    spr.setTextSize(4);
    spr.print(" C");
    spr.setTextSize(2);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    spr.setCursor((240 - spr.textWidth("Indoor Temp")) / 2, 140);
    spr.println("Indoor Temp");
}

void showCO2MonitorMenu() {
    spr.fillRect(0, 0, 240, 32, TFT_DARKGREEN);
    spr.setTextColor(TFT_WHITE, TFT_DARKGREEN);
    spr.setTextSize(2);
    spr.setCursor(60, 9);
    spr.println("CO2 Monitor");
    uint16_t co2Color = (currentCO2 > co2Threshold) ? TFT_RED : TFT_GREEN;
    if (currentCO2 <= 0) co2Color = TFT_WHITE;
    spr.setTextColor(co2Color, TFT_BLACK);
    String co2Str = String(currentCO2);
    spr.setTextSize(6);
    int16_t x = (240 - spr.textWidth(co2Str)) / 2;
    spr.setCursor(x, 70);
    spr.print(co2Str);
    spr.setTextSize(2);
    spr.setCursor(spr.getCursorX() + 5, 90);
    spr.print("ppm");
    spr.setTextSize(2);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    String status = (currentCO2 > co2Threshold) ? "VENTILATE!" : "NORMAL";
    if (currentCO2 <= 0) status = "READING...";
    spr.setCursor((240 - spr.textWidth(status)) / 2, 140);
    spr.println(status);
    spr.fillRect(0, 160, 240, 40, TFT_DARKGREY);
    spr.drawRect(0, 160, 240, 40, TFT_WHITE);
    spr.setTextColor(TFT_WHITE, TFT_DARKGREY);
    spr.setTextSize(2);
    if (readingCount > 0) {
        spr.setCursor(10, 170);
        spr.printf("Max:%5d", maxCO2);
        spr.setCursor(130, 170);
        spr.printf("Min:%5d", minCO2);
    }
}

void showVentilationMenu() {
    spr.fillRect(0, 0, 240, 32, TFT_DARKCYAN);
    spr.setTextColor(TFT_WHITE, TFT_DARKCYAN);
    spr.setTextSize(2);
    spr.setCursor(60, 9);
    spr.println("Ventilation");
    uint16_t ventColor;
    String ventStatus, recommendation;
    if (currentCO2 <= 0) { ventColor = TFT_WHITE; ventStatus = "UNKNOWN"; recommendation = "Waiting for data..."; }
    else if (currentCO2 < 800) { ventColor = TFT_GREEN; ventStatus = "GOOD"; recommendation = "Ventilation is OK"; }
    else if (currentCO2 < co2Threshold) { ventColor = TFT_YELLOW; ventStatus = "FAIR"; recommendation = "Consider Ventilation"; }
    else { ventColor = TFT_RED; ventStatus = "POOR"; recommendation = "Ventilation Needed!"; }
    spr.setTextColor(ventColor, TFT_BLACK);
    spr.setTextSize(5);
    spr.setCursor((240 - spr.textWidth(ventStatus)) / 2, 70);
    spr.print(ventStatus);
    spr.setTextSize(2);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    spr.setCursor((240 - spr.textWidth(recommendation)) / 2, 140);
    spr.print(recommendation);
}

void showMenuIndicator() {
    String menuNames[] = {"CO2 Monitor", "Temperature", "People Count", "Ventilation"};
    spr.setTextSize(1);
    spr.setTextColor(TFT_DARKGREY, TFT_BLACK);
    spr.setCursor(10, 225);
    spr.print("< " + menuNames[currentDisplayState] + " >");
}

void showReadyScreen() {
    spr.fillSprite(TFT_BLACK);
    spr.setTextColor(TFT_GREEN, TFT_BLACK);
    spr.setTextSize(4);
    spr.setCursor(60, 100);
    spr.println("READY!");
    spr.pushSprite(0, 0);
}

bool loadCredentials() {
    preferences.begin("net-config", true);
    bool ssidStored = preferences.getString("ssid", ssid, sizeof(ssid)) > 0;
    bool passStored = preferences.getString("password", password, sizeof(password)) > 0;
    bool ipStored = preferences.getString("server_ip", server_ip, sizeof(server_ip)) > 0;
    bool userStored = preferences.getString("username", username, sizeof(username)) > 0;
    bool userPassStored = preferences.getString("user_pass", user_password, sizeof(user_password)) > 0;
    preferences.end();
    if (ssidStored && passStored && ipStored && userStored && userPassStored) {
        serverName = "http://" + String(server_ip) + ":5933/update";
        return true;
    }
    return false;
}

void setupSoftAP() {
    const char* ap_ssid = "ESP32_Sensor_Setup";
    WiFi.softAP(ap_ssid);
    IPAddress apIP = WiFi.softAPIP();
    dnsServer.start(53, "*", apIP);
    webServer.on("/", HTTP_GET, handleRoot);
    webServer.on("/save", HTTP_POST, handleSave);
    webServer.onNotFound([]() {
        webServer.send(200, "text/html", "<html><body><h1>Redirecting...</h1><script>window.location.href='/';</script></body></html>");
    });
    webServer.begin();
}

void showConfigurationScreen() {
    spr.fillSprite(TFT_BLACK);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    spr.setTextSize(2);
    spr.setCursor(20, 40);
    spr.println("WiFi Setup Mode");
    spr.setTextSize(1);
    spr.setCursor(10, 80);
    spr.println("1. Connect to WiFi:");
    spr.setTextColor(TFT_CYAN);
    spr.setCursor(30, 100);
    spr.println("   'ESP32_Sensor_Setup'");
    spr.setTextColor(TFT_WHITE);
    spr.setCursor(10, 130);
    spr.println("2. A config page will open.");
    spr.setCursor(10, 160);
    spr.println("3. Enter your WiFi, Server IP,");
    spr.setCursor(30, 175);
    spr.println("and User credentials.");
    spr.pushSprite(0, 0);
}

void handleRoot() {
    String html = R"rawliteral(
<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>ESP32 Sensor Hub Setup</title><style>body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; } .container { background: rgba(255, 255, 255, 0.95); padding: 30px 40px; border-radius: 20px; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2); width: 90%; max-width: 500px; } h1, h2 { text-align: center; color: #2c3e50; } h2 { font-size: 1.2rem; margin-top: 25px; border-top: 1px solid #eee; padding-top: 25px; } label { display: block; margin-top: 15px; margin-bottom: 8px; color: #555; font-weight: 600; } input[type=text], input[type=password] { width: 100%; padding: 12px; margin: 0; box-sizing: border-box; border: 1px solid #ccc; border-radius: 8px; } input[type=submit] { width: 100%; padding: 12px; margin-top: 30px; background: linear-gradient(135deg, #3498db, #2980b9); color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 1rem; transition: all 0.3s ease; } input[type=submit]:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(52, 152, 219, 0.3); }</style></head><body><div class="container"><h1>Sensor Hub Configuration</h1><form action="/save" method="POST"><h2>WiFi Settings</h2><label for="ssid">WiFi SSID</label><input type="text" id="ssid" name="ssid" required><label for="password">WiFi Password</label><input type="password" id="password" name="password"><h2>Server Settings</h2><label for="server_ip">Server IP Address</label><input type="text" id="server_ip" name="server_ip" required><label for="username">Username</label><input type="text" id="username" name="username" required><label for="user_pass">Password</label><input type="password" id="user_pass" name="user_pass" required><input type="submit" value="Save and Connect"></form></div></body></html>
)rawliteral";
    webServer.send(200, "text/html", html);
}

void handleSave() {
    preferences.begin("net-config", false);
    preferences.putString("ssid", webServer.arg("ssid"));
    preferences.putString("password", webServer.arg("password"));
    preferences.putString("server_ip", webServer.arg("server_ip"));
    preferences.putString("username", webServer.arg("username"));
    preferences.putString("user_pass", webServer.arg("user_pass"));
    preferences.end();
    String response = "<h1>Settings Saved!</h1><p>The device will now reboot.</p>";
    webServer.send(200, "text/html", response);
    delay(2000);
    ESP.restart();
}
