import warnings

warnings.simplefilter(action='ignore', category=FutureWarning)

import cv2
import numpy as np
import torch
import time
from collections import deque, defaultdict
from flask import Flask, request, jsonify, Response, render_template_string, session, redirect, url_for, flash, \
    send_file
from io import BytesIO, StringIO
import threading
import logging
import socket
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import os
import secrets
from datetime import datetime, timedelta
import pandas as pd

# ========================
# 配置区
# ========================
INFERENCE_INTERVAL_FRAMES = 20
YOLO_MODEL_NAME = 'yolov5s'
MODEL_INPUT_SIZE = 640
MAX_TRACKING_DISTANCE = 150
USE_REAL_CAMERA = False
SAVE_LOG = False
LOG_FILE = "classroom_log.csv"
MONITOR_FILTER = True
FILTER_LIFESPAN_HOURS = 1000
CAMERA_INDEX = 0
DATABASE_FILE = 'users.db'

QUALITY_PRESETS = {
    'sd': {'quality': 50, 'fps': 15},
    'hd': {'quality': 80, 'fps': 20},
    'uhd': {'quality': 95, 'fps': 30}
}

# 全局共享数据
shared_data = {
    'people_count': 0, 'co2_level': 0, 'temperature': 0.0, 'ventilation_status': "OFF",
    'last_esp32_update_time': 0.0,
    'last_active_serial': 'N/A',
    'jpeg_quality': QUALITY_PRESETS['hd']['quality'],
    'target_fps': QUALITY_PRESETS['hd']['fps'],
    'lock': threading.Lock()
}

device_commands = {}
sd_card_data = {}
cmd_lock = threading.Lock()
sd_lock = threading.Lock()

output_frame = None
frame_lock = threading.Lock()
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)
app.secret_key = os.urandom(24)

# ========================
# HTML 网页端
# ========================
AUTH_TEMPLATE_STYLE = """<style>body{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);display:flex;justify-content:center;align-items:center;height:100vh;margin:0}.auth-container{background:rgba(255,255,255,.95);padding:40px;border-radius:20px;box-shadow:0 10px 40px rgba(0,0,0,.2);width:350px}h1{text-align:center;margin-bottom:25px;color:#2c3e50}.form-group{margin-bottom:20px}label{display:block;margin-bottom:8px;color:#555;font-weight:600}input{width:100%;padding:12px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px}button{width:100%;padding:12px;background:linear-gradient(135deg,#3498db,#2980b9);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:1rem;transition:all .3s ease}button:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(52,152,219,.3)}.alert{padding:12px;background-color:#f8d7da;color:#721c24;border:1px solid #f5c6cb;border-radius:8px;margin-bottom:20px;text-align:center}.alert.success{background-color:#d4edda;color:#155724;border-color:#c3e6cb}p{text-align:center;margin-top:20px;color:#555}a{color:#3498db;text-decoration:none}</style>"""
LOGIN_TEMPLATE = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>登录</title>{AUTH_TEMPLATE_STYLE}</head><body><div class="auth-container"><h1>系统登录</h1>{{% with messages = get_flashed_messages(with_categories=true) %}}{{% if messages %}}{{% for category, message in messages %}}<div class="alert {{ 'success' if category == 'success' else '' }}">{{ message }}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}<form method="post"><div class="form-group"><label for="username">用户名</label><input type="text" id="username" name="username" required></div><div class="form-group"><label for="password">密码</label><input type="password" id="password" name="password" required></div><button type="submit">登录</button></form><p>还没有账户? <a href="/register">立即注册</a></p></div></body></html>"""
REGISTER_TEMPLATE = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>注册</title>{AUTH_TEMPLATE_STYLE}</head><body><div class="auth-container"><h1>注册新账户</h1>{{% with messages = get_flashed_messages(with_categories=true) %}}{{% if messages %}}<div class="alert {{ 'success' if messages[0][0] == 'success' else '' }}">{{ messages[0][1] }}</div>{{% endif %}}{{% endwith %}}<form method="post"><div class="form-group"><label for="username">用户名</label><input type="text" id="username" name="username" required></div><div class="form-group"><label for="password">密码</label><input type="password" id="password" name="password" required></div><div class="form-group"><label for="email">电子邮箱</label><input type="email" id="email" name="email" placeholder="example@domain.com" required></div><button type="submit">注册</button></form><p>已有账户? <a href="/login">返回登录</a></p></div></body></html>"""

DEVICES_TEMPLATE = """
<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>设备管理</title>
<style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #333; margin: 0; padding: 20px; min-height: 100vh; }
    .container { max-width: 1200px; margin: 40px auto; background: rgba(255, 255, 255, 0.95); padding: 30px; border-radius: 20px; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2); }
    .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee; padding-bottom: 15px; margin-bottom: 25px; }
    h1 { color: #2c3e50; } a { color: #3498db; text-decoration: none; }
    .device-table { width: 100%; border-collapse: collapse; }
    .device-table th, .device-table td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #ddd; white-space: nowrap; }
    .device-table th { background-color: #f8f9fa; } .actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;}
    .action-btn { color: white; border: none; padding: 8px 12px; border-radius: 5px; cursor: pointer; text-decoration: none; display: inline-block; font-size: 14px;}
    .delete-btn { background-color: #e74c3c; } .read-btn { background-color: #3498db; } .settings-btn { background-color: #f39c12; }
    .contact-btn { background-color: #1abc9c; }
    .switch-btn { background-color: #9b59b6; } 
    .current-device-btn { background-color: #2ecc71; cursor: default; }
    .progress-bar { width: 100%; background-color: #ecf0f1; border-radius: 5px; overflow: hidden; height: 18px; box-shadow: inset 0 1px 3px rgba(0,0,0,.1); min-width: 120px; }
    .progress-bar-inner { height: 100%; background-color: #3498db; text-align: center; color: white; font-size: 12px; line-height: 18px; white-space: nowrap; transition: width 0.5s ease; }
    .modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.6); display: none; justify-content: center; align-items: center; z-index: 1000; }
    .modal-content { background: white; padding: 30px; border-radius: 15px; box-shadow: 0 5px 25px rgba(0,0,0,0.2); width: 90%; max-width: 400px; }
    .modal-content h2 { margin-top: 0; color: #2c3e50; }
    .modal-content label { display: block; margin: 15px 0 5px; font-weight: 600; }
    .modal-content input { width: 100%; padding: 10px; border-radius: 5px; border: 1px solid #ccc; box-sizing: border-box; }
    .modal-actions { margin-top: 25px; display: flex; justify-content: flex-end; gap: 10px; }
    .modal-actions button { padding: 10px 20px; border-radius: 5px; border: none; cursor: pointer; }
    .modal-save-btn { background-color: #27ae60; color: white; }
    .modal-cancel-btn { background-color: #ecf0f1; }
</style>
</head><body>
<div class="container">
    <div class="header"><h1>设备管理</h1><a href="/">返回监控主页</a></div>
    <table class="device-table">
        <thead><tr><th>设备序列号</th><th>上次连接</th><th>CO₂阈值</th><th>SD卡用量</th><th>联系人</th><th>电话</th><th>职位</th><th>操作</th></tr></thead>
        <tbody>
            {% for device in devices %}
            <tr>
                <td>{{ device.serial_number }}</td>
                <td>{{ device.last_seen_cst }}</td>
                <td>{{ device.co2_threshold }} ppm</td>
                <td>
                    {% if device.sd_total_mb > 0 %}
                        {% set usage_percent = (device.sd_used_mb / device.sd_total_mb * 100) | round(1) %}
                        <div class="progress-bar" title="{{ device.sd_used_mb | round(2) }} MB / {{ device.sd_total_mb | round(2) }} MB">
                            <div class="progress-bar-inner" style="width: {{ usage_percent }}%;">{{ usage_percent }}%</div>
                        </div>
                    {% else %} N/A {% endif %}
                </td>
                <td>{{ device.contact_name or 'N/A' }}</td>
                <td>{{ device.contact_phone or 'N/A' }}</td>
                <td>{{ device.contact_position or 'N/A' }}</td>
                <td class="actions">
                    {% if device.serial_number == selected_serial %}
                        <button class="action-btn current-device-btn" disabled>当前设备</button>
                    {% else %}
                        <form method="post" action="/devices/switch/{{ device.serial_number }}" style="margin:0;">
                           <button type="submit" class="action-btn switch-btn">切换设备</button>
                        </form>
                    {% endif %}
                    <button class="action-btn settings-btn" onclick="openThresholdModal('{{ device.serial_number }}', '{{ device.co2_threshold }}')">阈值</button>
                    <button class="action-btn contact-btn" onclick="openContactModal('{{ device.serial_number }}', '{{ device.contact_name or '' }}', '{{ device.contact_phone or '' }}', '{{ device.contact_position or '' }}')">联系人</button>
                    <a href="/devices/browse_sd/{{ device.serial_number }}" class="action-btn read-btn" target="_blank">SD卡</a>
                    <form method="post" action="/devices/delete/{{ device.serial_number }}" onsubmit="return confirm('确定要删除此设备吗？');" style="margin:0;"><button type="submit" class="action-btn delete-btn">删除</button></form>
                </td>
            </tr>
            {% else %}
            <tr><td colspan="8" style="text-align:center; padding: 20px;">没有已注册的设备。</td></tr>
            {% endfor %}
        </tbody>
    </table>
</div>
<div id="thresholdModal" class="modal-overlay">
    <div class="modal-content">
        <h2 id="modalTitle">设置 CO₂ 预警阈值</h2>
        <label for="thresholdInput">输入新的阈值 (ppm):</label>
        <input type="number" id="thresholdInput" placeholder="例如: 1000">
        <div class="modal-actions">
            <button class="modal-cancel-btn" onclick="closeAllModals()">取消</button>
            <button class="modal-save-btn" onclick="saveThreshold()">保存</button>
        </div>
    </div>
</div>
<div id="contactModal" class="modal-overlay">
    <div class="modal-content">
        <h2 id="contactModalTitle">编辑设备联系人</h2>
        <label for="contactNameInput">姓名:</label>
        <input type="text" id="contactNameInput">
        <label for="contactPhoneInput">电话:</label>
        <input type="text" id="contactPhoneInput">
        <label for="contactPositionInput">职位:</label>
        <input type="text" id="contactPositionInput">
        <div class="modal-actions">
            <button class="modal-cancel-btn" onclick="closeAllModals()">取消</button>
            <button class="modal-save-btn" onclick="saveContact()">保存</button>
        </div>
    </div>
</div>
<script>
    const thresholdModal = document.getElementById('thresholdModal');
    const contactModal = document.getElementById('contactModal');
    let currentSerial = '';

    function closeAllModals() {
        thresholdModal.style.display = 'none';
        contactModal.style.display = 'none';
    }

    function openThresholdModal(serial, currentThreshold) {
        currentSerial = serial;
        document.getElementById('modalTitle').textContent = `设置设备 ${serial} 的阈值`;
        document.getElementById('thresholdInput').value = currentThreshold;
        thresholdModal.style.display = 'flex';
    }
    async function saveThreshold() {
        const newThreshold = document.getElementById('thresholdInput').value;
        if (!newThreshold || newThreshold < 400) { alert('请输入一个有效的阈值 (建议大于400 ppm)。'); return; }
        try {
            const response = await fetch('/devices/set_threshold', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ serial_number: currentSerial, threshold: newThreshold })
            });
            const result = await response.json();
            if (result.success) {
                alert('阈值更新成功！页面将刷新以显示新值。');
                location.reload();
            } else { alert('错误: ' + result.error); }
        } catch (error) {
            alert('请求失败，请检查服务器连接。');
        } finally { closeAllModals(); }
    }

    function openContactModal(serial, name, phone, position) {
        currentSerial = serial;
        document.getElementById('contactModalTitle').textContent = `编辑设备 ${serial} 的联系人`;
        document.getElementById('contactNameInput').value = name;
        document.getElementById('contactPhoneInput').value = phone;
        document.getElementById('contactPositionInput').value = position;
        contactModal.style.display = 'flex';
    }
    async function saveContact() {
        const contactData = {
            serial_number: currentSerial,
            name: document.getElementById('contactNameInput').value,
            phone: document.getElementById('contactPhoneInput').value,
            position: document.getElementById('contactPositionInput').value,
        };
        try {
            const response = await fetch('/devices/set_contact', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(contactData)
            });
            const result = await response.json();
            if (result.success) {
                alert('联系人信息更新成功！');
                location.reload();
            } else { alert('错误: ' + result.error); }
        } catch (error) {
            alert('请求失败，请检查服务器连接。');
        } finally { closeAllModals(); }
    }

    window.onclick = function(event) {
        if (event.target == thresholdModal || event.target == contactModal) {
            closeAllModals();
        }
    }
</script>
</body></html>
"""

SD_BROWSER_TEMPLATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>SD卡文件浏览器 ({{ serial }})</title><style>body { font-family: 'Segoe UI', monospace; background-color: #f4f4f9; color: #333; margin: 0; padding: 20px; } .container { max-width: 800px; margin: auto; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); } h1, h2 { color: #2c3e50; border-bottom: 2px solid #667eea; padding-bottom: 10px;} h2 { font-size: 1.2rem; margin-top: 20px; border-bottom-style: dashed; } #status { font-style: italic; color: #555; margin-bottom: 20px; } .file-item { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; } .file-item:hover { background-color: #ecf0f1; border-radius: 4px; } .file-item a.filename { flex-grow: 1; padding: 8px; text-decoration: none; color: #3498db; } .file-item .actions { display: flex; gap: 8px; margin-right: 8px; visibility: hidden; opacity: 0; transition: opacity 0.2s; } .file-item:hover .actions { visibility: visible; opacity: 1; } .action-btn, .analysis-btn { color: white; border: none; padding: 5px 10px; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 12px; } .delete-btn { background-color: #e74c3c; } .download-btn { background-color: #27ae60; } .analysis-btn { background-color: #8e44ad; margin-top: 15px; padding: 10px 15px; font-size: 14px; } .analysis-container { border-top: 2px dotted #ccc; margin-top: 20px; padding-top: 10px; } pre { background: #2d2d2d; color: #f1f1f1; padding: 15px; border-radius: 5px; white-space: pre-wrap; word-wrap: break-word; font-size: 14px; max-height: 400px; overflow-y: auto; }</style></head><body><div class="container"><h1>设备 {{ serial }} 的SD卡文件</h1><div id="status">正在请求文件列表...</div><div id="fileList"></div><div class="analysis-container"><button id="analyzeBtn" class="analysis-btn" style="display:none;" onclick="runAnalysis()">报表分析</button></div><h2>文件内容</h2><pre id="content">点击上面的文件名以查看内容。</pre></div><script>const serial = '{{ serial }}'; let intervalId; let fileListForAnalysis = []; function runAnalysis() { window.open(`/devices/analyze/${serial}`, '_blank'); } async function postCommand(command, serial_num) { await fetch('/devices/command', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ serial: serial_num, command: command }) }); if (intervalId) clearInterval(intervalId); intervalId = setInterval(() => fetchResult(serial_num), 2000); } async function readFile(filename) { document.getElementById('status').textContent = `正在读取 ${filename}...`; await postCommand(`READ_FILE:${filename}`, serial); } async function deleteFile(filename) { if (confirm(`确定要永久删除文件 '${filename}' 吗？`)) { document.getElementById('status').textContent = `正在删除 ${filename}...`; await postCommand(`DELETE_FILE:${filename}`, serial); } } async function fetchResult(serial_num) { try { const response = await fetch(`/devices/get_sd_result/${serial_num}`); if (!response.ok) return; const result = await response.json(); if (result.status === 'ready') { if (result.data.type === 'list') { document.getElementById('status').textContent = '文件列表加载成功。'; const fileListDiv = document.getElementById('fileList'); let html = '<h2>文件列表</h2>'; if (result.data.files && result.data.files.length > 0) { fileListForAnalysis = result.data.files; result.data.files.forEach(file => { html += `<div class="file-item"><a href="#" class="filename" onclick="readFile('${file}'); return false;">${file}</a><div class="actions"><a href="/devices/download_sd_file/${serial}/${file}" class="action-btn download-btn">下载</a><button class="action-btn delete-btn" onclick="deleteFile('${file}')">删除</button></div></div>`; }); document.getElementById('analyzeBtn').style.display = 'block'; } else { html += '<p>SD卡根目录为空或无法读取。</p>'; document.getElementById('analyzeBtn').style.display = 'none'; } fileListDiv.innerHTML = html; clearInterval(intervalId); } else if (result.data.type === 'content') { document.getElementById('status').textContent = '文件内容加载成功。'; document.getElementById('content').textContent = result.data.content; clearInterval(intervalId); } } } catch (error) { document.getElementById('status').textContent = '轮询服务器时出错。'; clearInterval(intervalId); } } document.addEventListener('DOMContentLoaded', () => { postCommand('LIST_FILES', serial); });</script></body></html>"""
HTML_TEMPLATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>大教室智能通风控制系统</title><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css"><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><style>*{margin:0;padding:0;box-sizing:border-box}html{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh}body{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;color:#333;overflow-x:hidden;padding-top:20px}.container{max-width:1600px;margin:0 auto;padding:0 20px}.header-auth{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;color:#fff;padding:0 10px}.header-auth a{color:#f0f2f5;text-decoration:none;margin-left:15px}.header-auth .right-nav span{margin-left:15px}.main-grid{display:grid;grid-template-columns:1fr 2fr;gap:25px;align-items:start}.data-section,.video-section-wrapper{display:flex;flex-direction:column;gap:25px}.video-section,.chart-section{background:rgba(255,255,255,.95);border-radius:20px;padding:30px;box-shadow:0 10px 40px rgba(0,0,0,.1)}.section-title{font-size:1.5rem;color:#2c3e50;margin-bottom:20px;display:flex;align-items:center;gap:10px}.video-container{position:relative;border-radius:15px;overflow:hidden;box-shadow:0 8px 25px rgba(0,0,0,.2);background:#000;min-height:400px}img#videoElement{width:100%;height:auto;display:block}.video-overlay{position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);color:#fff;display:flex;align-items:center;justify-content:center;font-size:1.2rem;z-index:10}.status-indicator{position:absolute;top:15px;right:15px;width:12px;height:12px;border-radius:50%;background:#e74c3c;z-index:11}.status-indicator.connected{background:#2ecc71;animation:pulse 2s infinite}.data-card{background:rgba(255,255,255,.95);border-radius:20px;padding:25px;box-shadow:0 10px 40px rgba(0,0,0,.1)}.ai-card #ai_suggestion{line-height:1.6;color:#555}.chart-card{min-height:400px;display:flex;flex-direction:column}.chart-card .section-title{margin-bottom:10px}.chart-container{flex-grow:1;position:relative}canvas#realTimeChart{position:absolute;top:0;left:0;width:100%;height:100%}.metric{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}.metric:last-child{margin-bottom:0}.metric-label{display:flex;align-items:center;gap:10px;font-weight:600}.metric-value{font-size:1.3rem;font-weight:700}.co2-level.high .metric-value{color:#e74c3c;animation:warning 1s ease-in-out infinite alternate}.ventilation-status .metric-value.on,.esp32-link-status .metric-value.on,.filter-status .metric-value.good{color:#2ecc71}.ventilation-status .metric-value.off,.esp32-link-status .metric-value.off,.filter-status .metric-value.replace{color:#e74c3c}.filter-status .metric-value.warning{color:#f39c12}.control-button{background:linear-gradient(135deg,#3498db,#2980b9);color:#fff;border:none;padding:12px 24px;border-radius:8px;cursor:pointer;transition:all .3s ease}.control-button:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(52,152,219,.3)}.sidebar{height:100%;width:260px;position:fixed;z-index:1000;top:0;right:0;background-color:rgba(30,30,30,.95);backdrop-filter:blur(5px);overflow-x:hidden;padding-top:60px;opacity:0;visibility:hidden;transition:opacity .3s ease-out,visibility .3s ease-out}.sidebar.open{opacity:1;visibility:visible;box-shadow:-10px 0 20px rgba(0,0,0,.3)}.sidebar h3{padding:8px 8px 15px 32px;font-size:20px;color:#a0a0a0;display:block}.sidebar .sidebar-btn{padding:12px 15px;margin:8px 32px;text-decoration:none;font-size:16px;color:#f1f1f1;display:block;transition:.3s;background:0 0;border:1px solid #555;border-radius:5px;width:calc(100% - 64px);text-align:left;cursor:pointer}.sidebar .sidebar-btn:hover{color:#fff;border-color:#3498db;background-color:rgba(52,152,219,.2)}.sidebar .sidebar-btn.active{color:#fff;background-color:#3498db;border-color:#3498db;font-weight:700}.sidebar .close-btn{position:absolute;top:10px;right:25px;font-size:36px;color:#a0a0a0;text-decoration:none;transition:.3s}.sidebar .close-btn:hover{color:#fff}.overlay{height:100%;width:100%;position:fixed;z-index:999;top:0;left:0;background-color:rgba(0,0,0,.6);display:none;opacity:0;transition:opacity .3s ease-out}.overlay.visible{display:block;opacity:1}
.map-card .map-placeholder{display:flex;align-items:center;justify-content:center;min-height:200px;background-color:#ecf0f1;border-radius:15px;color:#7f8c8d;font-size:1.1rem;border:2px dashed #bdc3c7}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(1.1)}}@keyframes warning{from{text-shadow:none}to{text-shadow:0 0 10px rgba(231,76,60,.7)}}@media (max-width:1200px){.main-grid{grid-template-columns:1fr 1fr}.data-section{grid-column:1;grid-row:1}.video-section-wrapper{grid-column:2;grid-row:1}}@media (max-width:768px){.main-grid{grid-template-columns:1fr}}</style></head><body><div class="container"><div class="header-auth"><div id="current_device">当前设备: N/A</div><div class="right-nav"><span>服务器IP: <strong>{{ ip_address }}</strong></span><span>|</span><span>欢迎, <strong>{{ username }}</strong></span><a href="/devices">设备管理</a><a href="/logout">退出登录</a></div></div><div class="main-grid"><div class="data-section"><div class="data-card"><h2 class="section-title"><i class="fas fa-chart-line"></i> 实时监测数据</h2><div class="metric people-count"><div class="metric-label"><i class="fas fa-users"></i> 在场人数</div><div class="metric-value" id="people_count">0</div></div><div class="metric co2-level" id="co2Metric"><div class="metric-label"><i class="fas fa-cloud"></i> CO₂浓度</div><div class="metric-value" id="co2_level">0 ppm</div></div><div class="metric temperature-level"><div class="metric-label"><i class="fas fa-thermometer-half"></i> 室内温度</div><div class="metric-value" id="temperature">0.0 &deg;C</div></div><div class="metric ventilation-status"><div class="metric-label"><i class="fas fa-wind"></i> 通风状态</div><div class="metric-value off" id="ventilation_status">关闭</div></div></div><div class="data-card"><h2 class="section-title"><i class="fas fa-info-circle"></i> 系统状态</h2><div class="metric esp32-link-status"><div class="metric-label"><i class="fas fa-wifi"></i> 探测器连接</div><div class="metric-value off" id="esp32_link_status">丢失</div></div><div class="metric filter-status"><div class="metric-label"><i class="fas fa-filter"></i> 过滤器状态</div><div class="metric-value good" id="filter_status">良好 (0.0%)</div></div><div class="metric"><div class="metric-label"><i class="fas fa-clock"></i> 最后更新</div><div class="metric-value" id="last_update" style="font-size:1rem">--:--:--</div></div></div><div class="data-card ai-card"><h2 class="section-title"><i class="fas fa-robot"></i> 智能AI助手</h2><p id="ai_suggestion">正在分析数据...</p></div>
<div class="data-card map-card"><h2 class="section-title"><i class="fas fa-map-marked-alt"></i> 设备地图分布</h2><div class="map-placeholder">地图功能正在开发中...</div></div>
</div><div class="video-section-wrapper"><div class="video-section"><h2 class="section-title"><i class="fas fa-video"></i> 实时视频监控</h2><div class="video-container"><img id="videoElement" alt="视频加载中..."><div class="video-overlay" id="videoOverlay"><i class="fas fa-circle-notch fa-spin"></i>&nbsp; 连接中...</div><div class="status-indicator" id="connectionStatus"></div></div><div class="controls" style="margin-top:20px;display:flex;gap:10px"><button class="control-button" onclick="reconnectVideo()"><i class="fas fa-sync-alt"></i> 重新连接</button><button class="control-button" id="toggleVideoButton" onclick="toggleVideo()"><i class="fas fa-eye-slash"></i> 关闭监控</button><button class="control-button" id="qualityBtn"><i class="fas fa-cog"></i> 清晰度</button></div></div><div class="chart-section data-card chart-card"><h2 class="section-title"><i class="fas fa-chart-area"></i> 数据实时曲线图</h2><div class="chart-container"><canvas id="realTimeChart"></canvas></div></div></div></div><div id="qualitySidebar" class="sidebar"><a href="javascript:void(0)" class="close-btn" id="closeSidebarBtn">&times;</a><h3>选择清晰度</h3><button class="sidebar-btn" data-preset="sd">标清 (15fps)</button><button class="sidebar-btn active" data-preset="hd">高清 (20fps)</button><button class="sidebar-btn" data-preset="uhd">超清 (30fps)</button></div><div id="sidebarOverlay" class="overlay"></div><script>const aiSuggestionEl=document.getElementById("ai_suggestion"),videoElement=document.getElementById("videoElement"),videoOverlay=document.getElementById("videoOverlay"),connectionStatus=document.getElementById("connectionStatus"),peopleCountEl=document.getElementById("people_count"),co2LevelEl=document.getElementById("co2_level"),temperatureEl=document.getElementById("temperature"),ventilationStatusEl=document.getElementById("ventilation_status"),lastUpdateEl=document.getElementById("last_update"),co2Metric=document.getElementById("co2Metric"),esp32LinkStatusEl=document.getElementById("esp32_link_status"),filterStatusEl=document.getElementById("filter_status"),toggleVideoButton=document.getElementById("toggleVideoButton"),qualityBtn=document.getElementById("qualityBtn"),qualitySidebar=document.getElementById("qualitySidebar"),sidebarOverlay=document.getElementById("sidebarOverlay"),closeSidebarBtn=document.getElementById("closeSidebarBtn"),presetButtons=document.querySelectorAll(".sidebar-btn"),currentDeviceEl=document.getElementById("current_device");let dataUpdateInterval=null,reconnectTimeout=null,isVideoVisible=!0,realTimeChart=null,lastChartUpdateTime=0;const CHART_UPDATE_INTERVAL=1e4,MAX_CHART_POINTS=12;function startStream(){isVideoVisible&&(videoOverlay.style.display="flex",videoOverlay.innerHTML='<i class="fas fa-circle-notch fa-spin"></i>&nbsp; 连接中...',connectionStatus.classList.remove("connected"),videoElement.src=`/video_feed?timestamp=${(new Date).getTime()}`)}videoElement.onload=()=>{videoOverlay.style.display="none",connectionStatus.classList.add("connected"),reconnectTimeout&&clearTimeout(reconnectTimeout)},videoElement.onerror=()=>{isVideoVisible&&(videoOverlay.style.display="flex",videoOverlay.innerHTML='<i class="fas fa-exclamation-triangle"></i>&nbsp; 连接失败，5秒后重试...',connectionStatus.classList.remove("connected"),reconnectTimeout&&clearTimeout(reconnectTimeout),reconnectTimeout=setTimeout(startStream,5e3))};function reconnectVideo(){reconnectTimeout&&clearTimeout(reconnectTimeout),isVideoVisible||(isVideoVisible=!0,toggleVideoButton.innerHTML='<i class="fas fa-eye-slash"></i> 关闭监控'),startStream()}function toggleVideo(){isVideoVisible?(videoElement.src="",videoOverlay.style.display="flex",videoOverlay.innerHTML='<i class="fas fa-pause-circle"></i>&nbsp; 监控已暂停',connectionStatus.classList.remove("connected"),toggleVideoButton.innerHTML='<i class="fas fa-eye"></i> 打开监控',isVideoVisible=!1,reconnectTimeout&&(clearTimeout(reconnectTimeout),reconnectTimeout=null)):(isVideoVisible=!0,toggleVideoButton.innerHTML='<i class="fas fa-eye-slash"></i> 关闭监控',startStream())}function openSidebar(){qualitySidebar.classList.add("open"),sidebarOverlay.classList.add("visible")}function closeSidebar(){qualitySidebar.classList.remove("open"),sidebarOverlay.classList.remove("visible")}async function setStreamPreset(e){try{let t=await fetch("/set_preset",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({preset:e})});t.ok?(console.log(`清晰度预设已切换为: ${e.toUpperCase()}`),presetButtons.forEach(t=>{t.classList.toggle("active",t.dataset.preset===e)}),closeSidebar()):console.error("设置预设失败")}catch(e){console.error("设置预设请求错误:",e)}}async function fetchData(){try{let e=await fetch("/update");if(!e.ok)throw new Error("数据获取失败");let t=await e.json();updateDisplay(t)}catch(e){console.error("数据轮询失败:",e)}}function updateDisplay(e){aiSuggestionEl.textContent=e.ai_suggestion,currentDeviceEl.textContent="当前设备: "+e.current_device_serial,peopleCountEl.textContent=e.people_count,co2LevelEl.textContent=e.co2_level+" ppm",temperatureEl.innerHTML=e.temperature.toFixed(1)+" &deg;C",co2Metric.classList.toggle("high",e.co2_level>1e3);let t="ON"===e.ventilation_status?"开启":"关闭",o="ON"===e.ventilation_status?"on":"off";ventilationStatusEl.textContent=t,ventilationStatusEl.className=`metric-value ${o}`;let n="OK"===e.esp32_link_status?"正常":"丢失",a="OK"===e.esp32_link_status?"on":"off";esp32LinkStatusEl.textContent=n,esp32LinkStatusEl.className=`metric-value ${a}`;let i=`${e.filter_status_text} (${e.filter_usage.toFixed(1)}%)`;filterStatusEl.textContent=i,filterStatusEl.className=`metric-value ${e.filter_status_class}`,lastUpdateEl.textContent=(new Date).toLocaleTimeString();const l=Date.now();l-lastChartUpdateTime>=CHART_UPDATE_INTERVAL&&(updateChart(e),lastChartUpdateTime=l)}function initializeChart(){const e=document.getElementById("realTimeChart").getContext("2d");realTimeChart=new Chart(e,{type:"line",data:{labels:[],datasets:[{label:"CO₂浓度 (ppm)",data:[],borderColor:"rgba(231, 76, 60, 1)",backgroundColor:"rgba(231, 76, 60, 0.2)",yAxisID:"yCO2",tension:.4},{label:"在场人数",data:[],borderColor:"rgba(52, 152, 219, 1)",backgroundColor:"rgba(52, 152, 219, 0.2)",yAxisID:"yPeople",tension:.4,stepped:!0},{label:"室内温度 (°C)",data:[],borderColor:"rgba(243, 156, 18, 1)",backgroundColor:"rgba(243, 156, 18, 0.2)",yAxisID:"yTemp",tension:.4}]},options:{responsive:!0,maintainAspectRatio:!1,scales:{x:{ticks:{maxRotation:0,autoSkip:!0,maxTicksLimit:6}},yCO2:{type:"linear",display:!0,position:"left",title:{display:!0,text:"CO₂ (ppm)"}},yPeople:{type:"linear",display:!0,position:"right",title:{display:!0,text:"人数"},grid:{drawOnChartArea:!1},ticks:{stepSize:1,suggestedMin:0}},yTemp:{type:"linear",display:!0,position:"right",title:{display:!0,text:"温度 (°C)"},grid:{drawOnChartArea:!1},suggestedMin:10,suggestedMax:40}}}})}function updateChart(e){const t=(new Date).toLocaleTimeString("en-GB");realTimeChart.data.labels.push(t),realTimeChart.data.datasets[0].data.push(e.co2_level),realTimeChart.data.datasets[1].data.push(e.people_count),realTimeChart.data.datasets[2].data.push(e.temperature),realTimeChart.data.labels.length>MAX_CHART_POINTS&&(realTimeChart.data.labels.shift(),realTimeChart.data.datasets.forEach(e=>{e.data.shift()})),realTimeChart.update()}function init(){qualityBtn.addEventListener("click",openSidebar),closeSidebarBtn.addEventListener("click",closeSidebar),sidebarOverlay.addEventListener("click",closeSidebar),presetButtons.forEach(e=>{e.addEventListener("click",()=>{setStreamPreset(e.dataset.preset)})}),initializeChart(),startStream(),fetchData(),dataUpdateInterval=setInterval(fetchData,1e3)}document.addEventListener("DOMContentLoaded",init),window.addEventListener("beforeunload",()=>{dataUpdateInterval&&clearInterval(dataUpdateInterval),reconnectTimeout&&clearTimeout(reconnectTimeout),videoElement.src=""});</script></body></html>"""
ANALYSIS_TEMPLATE = """
<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>数据报表分析 ({{ serial }})</title>
<style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8f9fa; color: #333; margin: 0; padding: 20px; }
    .container { max-width: 900px; margin: auto; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
    h1, h2 { color: #2c3e50; border-bottom: 2px solid #667eea; padding-bottom: 10px; }
    h2 { font-size: 1.5rem; margin-top: 30px; }
    .report-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-top: 20px; }
    .report-card { background: #ecf0f1; padding: 20px; border-radius: 8px; }
    .report-card h3 { margin-top: 0; color: #34495e; }
    .report-card p { margin: 10px 0; font-size: 1rem; }
    .report-card .value { font-weight: bold; font-size: 1.2rem; color: #2980b9; }
    #status { padding: 15px; background-color: #eaf2f8; border-left: 5px solid #3498db; margin-top: 20px; border-radius: 5px; }
    .error { background-color: #fbeee6; border-left-color: #e74c3c; color: #c0392b; }
</style>
</head><body>
<div class="container">
    <h1>设备 {{ serial }} - 数据报表分析</h1>
    <div id="status">正在从设备获取并分析所有CSV数据...</div>

    <div id="daily-reports"><h2>日报</h2><div class="report-grid"></div></div>
    <div id="weekly-reports"><h2>周报</h2><div class="report-grid"></div></div>
    <div id="monthly-reports"><h2>月报</h2><div class="report-grid"></div></div>
</div>
<script>
    async function runAnalysis() {
        const serial = '{{ serial }}';
        const statusDiv = document.getElementById('status');
        try {
            const response = await fetch(`/devices/get_analysis_data/${serial}`);
            if (!response.ok) {
                throw new Error('无法获取分析数据，服务器响应异常。');
            }
            const data = await response.json();

            if (data.error) {
                throw new Error(data.error);
            }

            statusDiv.textContent = `分析完成。总共处理了 ${data.total_files} 个文件, ${data.total_records} 条记录。`;

            populateReports('daily-reports', data.reports.daily);
            populateReports('weekly-reports', data.reports.weekly);
            populateReports('monthly-reports', data.reports.monthly);

        } catch (error) {
            statusDiv.textContent = '分析失败: ' + error.message;
            statusDiv.className = 'error';
        }
    }

    function populateReports(sectionId, reports) {
        const section = document.getElementById(sectionId);
        const grid = section.querySelector('.report-grid');
        grid.innerHTML = ''; // Clear previous content

        const sortedKeys = Object.keys(reports).sort().reverse();

        if (sortedKeys.length === 0) {
            grid.innerHTML = '<p>暂无数据。</p>';
            return;
        }

        for (const key of sortedKeys) {
            const report = reports[key];
            const card = document.createElement('div');
            card.className = 'report-card';
            card.innerHTML = `
                <h3>${key}</h3>
                <p>平均CO₂浓度: <span class="value">${report.avg_co2.toFixed(1)} ppm</span></p>
                <p>超标次数: <span class="value">${report.exceedances} 次</span></p>
                <p>设备在线率: <span class="value">${report.uptime_percent.toFixed(1)}%</span></p>
            `;
            grid.appendChild(card);
        }
    }

    document.addEventListener('DOMContentLoaded', runAnalysis);
</script>
</body></html>
"""

def generate_ai_suggestion(co2, temp):
    """
    根据 CO2 和温度数据生成建议。
    """
    if co2 <= 0:
        return "正在等待传感器数据..."

    suggestion = ""
    if co2 > 1500:
        suggestion += "CO₂浓度极高, 强烈建议立即开窗通风, 并检查新风系统是否全功率运行。"
    elif co2 > 1000:
        suggestion += "CO₂浓度偏高, 空气质量不佳, 建议开启通风系统或开窗换气。"
    elif co2 > 800:
        suggestion += "CO₂浓度处于中等水平, 建议适时增加通风, 保持空气流通。"
    elif co2 > 600:
        suggestion += "当前CO₂浓度良好, 空气质量不错。"
    else:
        suggestion += "当前CO₂浓度极佳, 室内空气非常清新。"

    if temp > 26:
        suggestion += " 同时, 室内温度偏高, 可考虑开启空调或风扇辅助降温。"
    elif temp < 18:
        suggestion += " 同时, 室内温度偏低, 请注意保暖。"

    return suggestion


# ========================
# 数据库管理
# ========================
def init_db():
    if not os.path.exists(DATABASE_FILE):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE users
                          (
                              id            INTEGER PRIMARY KEY AUTOINCREMENT,
                              username      TEXT UNIQUE NOT NULL,
                              password_hash TEXT        NOT NULL,
                              email         TEXT UNIQUE
                          );''')
        cursor.execute('''CREATE TABLE devices
                          (
                              id            INTEGER PRIMARY KEY AUTOINCREMENT,
                              serial_number TEXT    NOT NULL,
                              user_id       INTEGER NOT NULL,
                              last_seen     TEXT,
                              api_token     TEXT UNIQUE,
                              co2_threshold INTEGER DEFAULT 1000,
                              FOREIGN KEY (user_id) REFERENCES users (id),
                              UNIQUE (serial_number, user_id)
                          );''')
        conn.commit()
        conn.close()


def migrate_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA table_info(devices)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'sd_total_mb' not in columns:
            cursor.execute("ALTER TABLE devices ADD COLUMN sd_total_mb INTEGER DEFAULT 0")
        if 'sd_used_mb' not in columns:
            cursor.execute("ALTER TABLE devices ADD COLUMN sd_used_mb INTEGER DEFAULT 0")
        if 'co2_threshold' not in columns:
            cursor.execute("ALTER TABLE devices ADD COLUMN co2_threshold INTEGER DEFAULT 1000")
        if 'contact_name' not in columns:
            cursor.execute("ALTER TABLE devices ADD COLUMN contact_name TEXT")
        if 'contact_phone' not in columns:
            cursor.execute("ALTER TABLE devices ADD COLUMN contact_phone TEXT")
        if 'contact_position' not in columns:
            cursor.execute("ALTER TABLE devices ADD COLUMN contact_position TEXT")
    finally:
        conn.close()


def check_esp32_credentials(username, password):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))
    user_record = cursor.fetchone()
    conn.close()
    if user_record and check_password_hash(user_record[1], password):
        return user_record[0]
    return None


# ========================
# Flask 路由
# ========================
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1));
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


@app.route('/')
def index():
    if 'username' not in session: return redirect(url_for('login'))
    return render_template_string(HTML_TEMPLATE, ip_address=get_local_ip(), username=session['username'])


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username, password = request.form['username'], request.form['password']
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor();
        cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
        user_record = cursor.fetchone();
        conn.close()
        if user_record and check_password_hash(user_record[0], password):
            session['username'] = username;
            return redirect(url_for('index'))
        else:
            flash('用户名或密码无效', 'danger')
    return render_template_string(LOGIN_TEMPLATE)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username, password, email = request.form['username'], request.form['password'], request.form['email']
        hashed_password = generate_password_hash(password, method="pbkdf2:sha256")
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor();
            cursor.execute("INSERT INTO users (username, password_hash, email) VALUES (?, ?, ?)",
                           (username, hashed_password, email))
            conn.commit();
            conn.close()
            flash('注册成功！请登录。', 'success');
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('该用户名或邮箱已被注册。', 'danger')
    return render_template_string(REGISTER_TEMPLATE)


@app.route('/logout')
def logout():
    session.pop('username', None);
    return redirect(url_for('login'))


@app.route('/api/login', methods=['POST'])
def api_login():
    if not request.is_json: return jsonify(error="Invalid request: Content-Type must be application/json"), 400
    data = request.get_json()
    username, password, serial_number = data.get('user'), data.get('pass'), data.get('serial')
    if not all([username, password, serial_number]): return jsonify(error="Missing credentials"), 400
    user_id = check_esp32_credentials(username, password)
    if not user_id: return jsonify(error="Invalid user credentials"), 401
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM devices WHERE serial_number = ? AND user_id = ?", (serial_number, user_id))
    if not cursor.fetchone(): conn.close(); return jsonify(error="Device not registered to this user"), 403
    api_token = secrets.token_hex(32)
    cursor.execute("UPDATE devices SET api_token = ? WHERE serial_number = ?", (api_token, serial_number))
    conn.commit();
    conn.close()
    return jsonify(token=api_token), 200


@app.route('/update', methods=['GET'])
def update_data():
    if 'username' in session:
        return get_display_data()

    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '): return jsonify(error="Auth header missing"), 401
    token = auth_header.split(' ')[1]
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT serial_number, co2_threshold FROM devices WHERE api_token = ?", (token,))
    device_data = cursor.fetchone()
    if not device_data: conn.close(); return jsonify(error="Invalid API Token"), 401

    serial_number, co2_threshold = device_data
    current_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("UPDATE devices SET last_seen = ? WHERE serial_number = ?", (current_time, serial_number))
    conn.commit()

    if 'co2' in request.args:
        sd_total_bytes = request.args.get('sd_total', default=0, type=int)
        sd_used_bytes = request.args.get('sd_used', default=0, type=int)
        sd_total_mb = sd_total_bytes / (1024 * 1024) if sd_total_bytes > 0 else 0
        sd_used_mb = sd_used_bytes / (1024 * 1024) if sd_used_bytes > 0 else 0

        cursor.execute("UPDATE devices SET sd_total_mb = ?, sd_used_mb = ? WHERE serial_number = ?",
                       (sd_total_mb, sd_used_mb, serial_number))
        conn.commit()

        with shared_data['lock']:
            shared_data['co2_level'] = request.args.get('co2', default=0, type=int)
            shared_data['temperature'] = request.args.get('temp', default=0.0, type=float)
            shared_data['ventilation_status'] = request.args.get('ventilation', default="UNKNOWN", type=str)
            shared_data['last_esp32_update_time'] = time.time()
            shared_data['last_active_serial'] = serial_number
        conn.close()
        return jsonify(status="ok")
    else:
        with cmd_lock:
            command = device_commands.pop(serial_number, "NONE")
        with shared_data['lock']:
            shared_data['last_esp32_update_time'] = time.time()
            shared_data['last_active_serial'] = serial_number
            response_data = {
                "people_count": shared_data['people_count'],
                "command": command,
                "filter_status": shared_data.get('filter_status_text', 'Unknown'),
                "co2_threshold": co2_threshold
            }
        conn.close()
        return jsonify(response_data)


@app.route('/devices/register', methods=['POST'])
def register_device():
    username, password, serial = request.form.get('user'), request.form.get('pass'), request.form.get('serial')
    if not all([username, password, serial]): return "Missing parameters", 400
    user_id = check_esp32_credentials(username, password)
    if not user_id: return "User Unauthorized", 401
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    current_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    try:
        cursor.execute(
            "INSERT OR REPLACE INTO devices (serial_number, user_id, last_seen, api_token) VALUES (?, ?, ?, NULL)",
            (serial, user_id, current_time))
        conn.commit();
        conn.close()
        return "Device registered", 200
    except sqlite3.Error:
        conn.close();
        return "Server error", 500


@app.route('/devices')
def devices_page():
    if 'username' not in session: return redirect(url_for('login'))
    conn = sqlite3.connect(DATABASE_FILE);
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (session['username'],))
    user = cursor.fetchone()
    if not user: return redirect(url_for('logout'))

    cursor.execute(
        "SELECT serial_number, last_seen, sd_used_mb, sd_total_mb, co2_threshold, contact_name, contact_phone, contact_position FROM devices WHERE user_id = ?",
        (user['id'],))
    devices_db = cursor.fetchall();
    conn.close()

    selected_serial = session.get('selected_device_serial')


    devices_processed = []
    for dev in devices_db:
        if dev['last_seen']:
            try:
                utc_time = datetime.strptime(dev['last_seen'], '%Y-%m-%d %H:%M:%S')
                cst_time = utc_time + timedelta(hours=8)
                cst_str = cst_time.strftime('%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                cst_str = "Invalid Date"
        else:
            cst_str = "Never"

        devices_processed.append({
            'serial_number': dev['serial_number'],
            'last_seen_cst': cst_str,
            'sd_used_mb': dev['sd_used_mb'] if dev['sd_used_mb'] is not None else 0,
            'sd_total_mb': dev['sd_total_mb'] if dev['sd_total_mb'] is not None else 0,
            'co2_threshold': dev['co2_threshold'] if dev['co2_threshold'] is not None else 1000,
            'contact_name': dev['contact_name'],
            'contact_phone': dev['contact_phone'],
            'contact_position': dev['contact_position']
        })

    return render_template_string(DEVICES_TEMPLATE, devices=devices_processed, selected_serial=selected_serial)



@app.route('/devices/delete/<serial_number>', methods=['POST'])
def delete_device(serial_number):
    if 'username' not in session: return redirect(url_for('login'))
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (session['username'],))
    user = cursor.fetchone()
    if user:
        cursor.execute("DELETE FROM devices WHERE serial_number = ? AND user_id = ?", (serial_number, user[0]));
        conn.commit();
    conn.close();
    return redirect(url_for('devices_page'))


@app.route('/devices/switch/<serial_number>', methods=['POST'])
def switch_device(serial_number):
    if 'username' not in session:
        return redirect(url_for('login'))

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (session['username'],))
    user = cursor.fetchone()
    if user:
        cursor.execute("SELECT id FROM devices WHERE serial_number = ? AND user_id = ?", (serial_number, user[0]))
        device = cursor.fetchone()
        if device:
            session['selected_device_serial'] = serial_number
    conn.close()

    return redirect(url_for('index'))




@app.route('/devices/set_threshold', methods=['POST'])
def set_threshold():
    if 'username' not in session:
        return jsonify(success=False, error="Unauthorized"), 401

    data = request.get_json()
    serial_number = data.get('serial_number')
    threshold = data.get('threshold')

    if not serial_number or not threshold:
        return jsonify(success=False, error="Missing parameters"), 400

    try:
        threshold_val = int(threshold)
    except ValueError:
        return jsonify(success=False, error="Invalid threshold value"), 400

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (session['username'],))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return jsonify(success=False, error="User not found"), 403

    cursor.execute("UPDATE devices SET co2_threshold = ? WHERE serial_number = ? AND user_id = ?",
                   (threshold_val, serial_number, user[0]))
    conn.commit()

    if cursor.rowcount == 0:
        conn.close()
        return jsonify(success=False, error="Device not found or permission denied"), 404

    conn.close()
    return jsonify(success=True)


@app.route('/devices/set_contact', methods=['POST'])
def set_contact():
    if 'username' not in session:
        return jsonify(success=False, error="Unauthorized"), 401

    data = request.get_json()
    serial_number = data.get('serial_number')
    name = data.get('name')
    phone = data.get('phone')
    position = data.get('position')

    if not serial_number:
        return jsonify(success=False, error="Missing serial number"), 400

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (session['username'],))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return jsonify(success=False, error="User not found"), 403

    cursor.execute("""
                   UPDATE devices
                   SET contact_name     = ?,
                       contact_phone    = ?,
                       contact_position = ?
                   WHERE serial_number = ?
                     AND user_id = ?
                   """, (name, phone, position, serial_number, user[0]))
    conn.commit()

    if cursor.rowcount == 0:
        conn.close()
        return jsonify(success=False, error="Device not found or permission denied"), 404

    conn.close()
    return jsonify(success=True)


def get_display_data():
    with shared_data['lock']:
        esp32_status = "OK" if (time.time() - shared_data.get('last_esp32_update_time', 0)) < 10 else "LOST"
        data_copy = {"people_count": shared_data['people_count'], "co2_level": shared_data['co2_level'],
                     "temperature": shared_data.get('temperature', 0.0),
                     "ventilation_status": shared_data['ventilation_status'], "esp32_link_status": esp32_status,
                     "filter_usage": shared_data.get('filter_usage', 0.0),
                     "filter_status_text": shared_data.get('filter_status_text', '未知'),
                     "filter_status_class": shared_data.get('filter_status_class', '')}

        selected_serial = session.get('selected_device_serial')
        data_copy['current_device_serial'] = selected_serial if selected_serial else shared_data.get(
            'last_active_serial', 'N/A')

        data_copy['ai_suggestion'] = generate_ai_suggestion(data_copy['co2_level'], data_copy['temperature'])
    return jsonify(data_copy)


@app.route('/devices/browse_sd/<serial_number>')
def browse_sd_card(serial_number):
    if 'username' not in session: return redirect(url_for('login'))
    return render_template_string(SD_BROWSER_TEMPLATE, serial=serial_number)


@app.route('/devices/command', methods=['POST'])
def device_command():
    if 'username' not in session: return jsonify(status="error", error="Unauthorized"), 401
    data = request.get_json()
    serial, command = data.get('serial'), data.get('command')
    if not all([serial, command]): return jsonify(status="error", error="Missing params"), 400
    with cmd_lock:
        with sd_lock: sd_card_data.pop(serial, None)
        device_commands[serial] = command;
    return jsonify(status="ok")


@app.route('/devices/get_sd_result/<serial_number>')
def get_sd_result(serial_number):
    if 'username' not in session: return jsonify(status="error", error="Unauthorized"), 401
    with sd_lock:
        data = sd_card_data.get(serial_number)
    if data is not None:
        return jsonify(status="ready", data=data)
    else:
        return jsonify(status="pending")


@app.route('/devices/upload_sd_data', methods=['POST'])
def upload_sd_data():
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '): return jsonify(error="Auth invalid"), 401
    token = auth_header.split(' ')[1]
    conn = sqlite3.connect(DATABASE_FILE);
    cursor = conn.cursor()
    cursor.execute("SELECT serial_number FROM devices WHERE api_token = ?", (token,));
    device = cursor.fetchone();
    conn.close()
    if not device: return jsonify(error="Invalid Token"), 401
    serial_number = device[0]
    if not request.is_json: return jsonify(error="Invalid request"), 400
    with sd_lock:
        sd_card_data[serial_number] = request.get_json()
    return jsonify(status="ok")


@app.route('/devices/download_sd_file/<serial>/<filename>')
def download_sd_file(serial, filename):
    if 'username' not in session: return "Unauthorized", 401
    with cmd_lock:
        with sd_lock: sd_card_data.pop(serial, None)
        device_commands[serial] = f"READ_FILE:{filename}"
    start_time = time.time()
    while time.time() - start_time < 20:
        with sd_lock:
            data = sd_card_data.get(serial)
        if data and data.get('type') == 'content':
            content = data.get('content', '')
            if content.startswith('Error:'): return f"Could not download: {content}", 404
            return send_file(BytesIO(content.encode('utf-8')), mimetype='application/octet-stream', as_attachment=True,
                             download_name=filename)
        time.sleep(1)
    return "Failed to retrieve file: request timed out.", 504


@app.route('/set_preset', methods=['POST'])
def set_preset():
    if 'username' not in session: return jsonify(success=False, message="Unauthorized"), 401
    global shared_data
    try:
        preset_name = request.json.get('preset', 'hd')
        preset = QUALITY_PRESETS.get(preset_name, QUALITY_PRESETS['hd'])
        with shared_data['lock']:
            shared_data['jpeg_quality'] = preset['quality'];
            shared_data['target_fps'] = preset['fps']
        return jsonify(success=True, preset=preset_name)
    except Exception:
        return jsonify(success=False, message="Invalid preset"), 400


def generate_frames():
    global output_frame, frame_lock
    while True:
        with frame_lock:
            if output_frame is None: time.sleep(0.01); continue
            frame_bytes = output_frame
        yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')


@app.route('/video_feed')
def video_feed():
    if 'username' not in session: return Response("Unauthorized", status=401)
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/devices/analyze/<serial>')
def analyze_data(serial):
    if 'username' not in session:
        return "Unauthorized", 401
    return render_template_string(ANALYSIS_TEMPLATE, serial=serial)


@app.route('/devices/get_analysis_data/<serial>')
def get_analysis_data(serial):
    if 'username' not in session:
        return jsonify(error="Unauthorized"), 401

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM devices WHERE serial_number=?", (serial,))
    device_owner = cursor.fetchone()
    cursor.execute("SELECT id FROM users WHERE username=?", (session['username'],))
    current_user = cursor.fetchone()

    if not device_owner or not current_user or device_owner[0] != current_user[0]:
        conn.close()
        return jsonify(error="Device not found or permission denied"), 403

    cursor.execute("SELECT co2_threshold FROM devices WHERE serial_number=?", (serial,))
    threshold = cursor.fetchone()[0]
    conn.close()

    with cmd_lock:
        with sd_lock: sd_card_data.pop(serial, None)
        device_commands[serial] = "LIST_FILES"

    all_files = []
    start_time = time.time()
    while time.time() - start_time < 10:
        with sd_lock:
            data = sd_card_data.get(serial)
            if data and data.get('type') == 'list':
                all_files = [f for f in data.get('files', []) if f.endswith('.csv')]
                break
        time.sleep(1)

    if not all_files:
        return jsonify(error="No CSV files found on the device or device did not respond.")

    all_content = ""
    for filename in all_files:
        with cmd_lock:
            with sd_lock: sd_card_data.pop(serial, None)
            device_commands[serial] = f"READ_FILE:{filename}"

        file_content = ""
        start_time = time.time()
        while time.time() - start_time < 15:
            with sd_lock:
                data = sd_card_data.get(serial)
                if data and data.get('type') == 'content':
                    file_content = data.get('content', '')
                    if not file_content.startswith('Error:'):
                        all_content += file_content
                    break
            time.sleep(1)

    if not all_content:
        return jsonify(error="Could not read content from any CSV file.")

    try:
        df = pd.read_csv(StringIO(all_content))
        df = df[df['Timestamp'].str.contains("UPTIME") == False]
        df = df[df['Timestamp'] != 'Timestamp']

        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
        df['CO2'] = pd.to_numeric(df['CO2'], errors='coerce')
        df.dropna(subset=['Timestamp', 'CO2'], inplace=True)
        if df.empty:
            return jsonify(error="No valid data records found in CSV files.")

        df['exceeds_threshold'] = df['CO2'] > threshold
        df['is_online'] = df['LinkStatus'] == 'OK'

        daily = df.groupby(df['Timestamp'].dt.date)
        weekly = df.groupby([df['Timestamp'].dt.isocalendar().year, df['Timestamp'].dt.isocalendar().week])
        monthly = df.groupby([df['Timestamp'].dt.year, df['Timestamp'].dt.month])

        def process_group(group):
            return {
                'avg_co2': float(group['CO2'].mean()),
                'exceedances': int(group['exceeds_threshold'].sum()),
                'uptime_percent': float((group['is_online'].sum() / len(group) * 100)) if len(group) > 0 else 0.0
            }

        daily_reports = {str(name): process_group(group) for name, group in daily}
        weekly_reports = {f"{name[0]}-W{name[1]}": process_group(group) for name, group in weekly}
        monthly_reports = {f"{name[0]}-{name[1]:02d}": process_group(group) for name, group in monthly}

        return jsonify({
            "total_files": len(all_files),
            "total_records": len(df),
            "reports": {
                "daily": daily_reports,
                "weekly": weekly_reports,
                "monthly": monthly_reports
            }
        })

    except Exception as e:
        return jsonify(error=f"Data processing failed: {str(e)}")


# ========================
# 核心模块
# ========================
class PeopleCounter:
    def __init__(self, model_name='yolov5m'):
        print(f"正在加载YOLOv5模型: {model_name}...")
        try:
            self.model = torch.hub.load('ultralytics/yolov5', model_name, pretrained=True);
        except Exception as e:
            print(f"模型加载失败: {e}");
            self.model = None
        if self.model: self.model.conf = 0.3; self.model.iou = 0.45; self.model.classes = [0]
        self.track_history = {};
        self.next_id = 0;
        self.max_history_len = 30;
        self.max_age = 30;
        self.people_count = 0

    def process_frame(self, frame, frame_count):
        if not self.model: cv2.putText(frame, "Model Load Failed", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 255),
                                       3); return frame
        detections = []
        if frame_count % INFERENCE_INTERVAL_FRAMES == 0:
            resized_frame = self.resize_frame(frame, MODEL_INPUT_SIZE)
            results = self.model(resized_frame)
            scaled_results = self.scale_coords(results.xyxy[0].cpu().numpy(), resized_frame.shape, frame.shape)
            for det in scaled_results: x1, y1, x2, y2 = map(int, det[:4]); w, h = x2 - x1, y2 - y1; detections.append(
                ((x1 + w // 2, y1 + h // 2), (w, h), det[4]))
        self.update_tracker(detections, frame_count);
        display_frame = self.draw_tracks(frame.copy());
        self.people_count = len(self.track_history)
        return display_frame

    def update_tracker(self, detections, frame_count):
        try:
            from scipy.optimize import linear_sum_assignment
        except ImportError:
            if frame_count % 100 == 0: print("警告: 未找到scipy, 追踪功能受限。请运行 'pip install scipy'。")
            self.track_history.clear()
            if detections:
                for i, det in enumerate(detections): self.track_history[i] = {
                    'positions': deque([det[0]], maxlen=self.max_history_len), 'last_seen': frame_count, 'bbox': det[1]}
            return
        if not self.track_history:
            for det in detections: self.track_history[self.next_id] = {
                'positions': deque([det[0]], maxlen=self.max_history_len), 'last_seen': frame_count,
                'bbox': det[1]}; self.next_id += 1
            return
        if not detections:
            for track_id in list(self.track_history.keys()):
                if frame_count - self.track_history[track_id]['last_seen'] > self.max_age: del self.track_history[
                    track_id]
            return
        track_ids, last_pos = list(self.track_history.keys()), [self.track_history[tid]['positions'][-1] for tid in
                                                                list(self.track_history.keys())]
        det_centers = [d[0] for d in detections]
        cost_matrix = np.array(
            [[np.linalg.norm(np.array(pos) - np.array(center)) for center in det_centers] for pos in last_pos])
        row_ind, col_ind = linear_sum_assignment(cost_matrix);
        matched_track_ids = set()
        for r, c in zip(row_ind, col_ind):
            if cost_matrix[r, c] < MAX_TRACKING_DISTANCE:
                track_id = track_ids[r];
                self.track_history[track_id]['positions'].append(detections[c][0]);
                self.track_history[track_id]['last_seen'] = frame_count;
                self.track_history[track_id]['bbox'] = detections[c][1];
                matched_track_ids.add(track_id)
        for i, det in enumerate(detections):
            if i not in col_ind: self.track_history[self.next_id] = {
                'positions': deque([det[0]], maxlen=self.max_history_len), 'last_seen': frame_count,
                'bbox': det[1]}; self.next_id += 1
        for track_id in track_ids:
            if track_id not in matched_track_ids and frame_count - self.track_history[track_id][
                'last_seen'] > self.max_age: del self.track_history[track_id]

    def draw_tracks(self, frame):
        for track_id, data in self.track_history.items():
            center, (w, h) = data['positions'][-1], data['bbox'];
            x1, y1, x2, y2 = int(center[0] - w / 2), int(center[1] - h / 2), int(center[0] + w / 2), int(
                center[1] + h / 2)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2);
            cv2.putText(frame, f"ID:{track_id}", (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        return frame

    def resize_frame(self, frame, target_size):
        h, w, _ = frame.shape;
        scale = target_size / max(h, w);
        new_w, new_h = int(w * scale), int(h * scale);
        return cv2.resize(frame, (new_w, new_h))

    def scale_coords(self, detections, resized_shape, original_shape):
        h_orig, w_orig = original_shape[:2];
        h_res, w_res = resized_shape[:2];
        scale_x, scale_y = w_orig / w_res, h_orig / h_res
        for det in detections: det[:4] = int(det[0] * scale_x), int(det[1] * scale_y), int(det[2] * scale_x), int(
            det[3] * scale_y)
        return detections


class FilterMonitor:
    def __init__(self, lifespan_hours=FILTER_LIFESPAN_HOURS):
        self.initial_lifespan_seconds = lifespan_hours * 3600;
        self.used_seconds = 0.0;
        self.last_check_time = time.time();
        self.filter_status = "GOOD"

    def update_filter_status(self, ventilation_is_on):
        current_time = time.time()
        if ventilation_is_on: self.used_seconds += (current_time - self.last_check_time)
        self.last_check_time = current_time;
        usage_percentage = min((self.used_seconds / self.initial_lifespan_seconds) * 100, 100.0)
        if usage_percentage >= 80:
            self.filter_status = "REPLACE"
        elif usage_percentage >= 60:
            self.filter_status = "WARNING"
        else:
            self.filter_status = "GOOD"
        return usage_percentage, self.filter_status


class DataLogger:
    def __init__(self, filename):
        self.filename = filename
        try:
            with open(self.filename, 'w') as f:
                f.write("Timestamp,PeopleCount,CO2Level,Temperature,VentilationStatus\n")
        except IOError as e:
            print(f"初始化日志文件错误 {self.filename}: {e}")

    def log_data(self, people, co2, temp, vent_status):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.filename, 'a') as f:
                f.write(f"{timestamp},{people},{co2},{temp:.1f},{vent_status}\n")
        except IOError as e:
            print(f"写入日志文件错误 {self.filename}: {e}")


def process_video():
    global shared_data, output_frame, frame_lock
    video_source = CAMERA_INDEX if USE_REAL_CAMERA else "classroom_video.mp4"
    cap = cv2.VideoCapture(video_source)

    if not cap.isOpened():
        print(f"错误: 视频源 '{video_source}' 打开失败。")
        error_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(error_frame, "Error: Cannot open video source.", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1,
                    (0, 0, 255), 2)
        cv2.putText(error_frame, "Check video file or system codecs.", (50, 280), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (0, 0, 255), 2)
        with frame_lock:
            _, encodedImage = cv2.imencode(".jpg", error_frame)
            output_frame = encodedImage.tobytes()
        return

    people_counter = PeopleCounter(model_name=YOLO_MODEL_NAME);
    filter_monitor = FilterMonitor() if MONITOR_FILTER else None;
    logger = DataLogger(LOG_FILE) if SAVE_LOG else None
    frame_count = 0;
    last_log_time = 0
    while True:
        loop_start_time = time.time();
        ret, frame = cap.read()
        if not ret:
            if not USE_REAL_CAMERA:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0);
                continue
            else:
                break
        display_frame = people_counter.process_frame(frame, frame_count);
        frame_count += 1
        with shared_data['lock']:
            shared_data['people_count'] = people_counter.people_count
            jpeg_quality, target_fps = shared_data.get('jpeg_quality', 80), shared_data.get('target_fps', 20)
            if filter_monitor:
                usage, status = filter_monitor.update_filter_status(shared_data['ventilation_status'] == "ON")
                shared_data['filter_usage'] = usage;
                status_map = {"GOOD": "良好", "WARNING": "警告", "REPLACE": "更换"};
                class_map = {"GOOD": "good", "WARNING": "warning", "REPLACE": "replace"}
                shared_data['filter_status_text'] = status_map.get(status, "未知");
                shared_data['filter_status_class'] = class_map.get(status, "")
        cv2.putText(display_frame, f"People: {people_counter.people_count}", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                    (255, 255, 0), 3)
        current_time = time.time()
        if logger and (current_time - last_log_time) >= 10.0: logger.log_data(people_counter.people_count,
                                                                              shared_data['co2_level'],
                                                                              shared_data['temperature'], shared_data[
                                                                                  'ventilation_status']); last_log_time = current_time
        with frame_lock:
            _, encodedImage = cv2.imencode(".jpg", display_frame, [int(cv2.IMWRITE_JPEG_QUALITY),
                                                                   jpeg_quality]);
            output_frame = encodedImage.tobytes()
        if target_fps > 0:
            process_time = time.time() - loop_start_time;
            sleep_duration = (1.0 / target_fps) - process_time
            if sleep_duration > 0: time.sleep(sleep_duration)
    cap.release()


# ========================
# 程序入口
# ========================
if __name__ == "__main__":
    init_db()
    migrate_db()
    video_thread = threading.Thread(target=process_video, daemon=True)
    video_thread.start()
    local_ip = get_local_ip()
    print("=" * 50)
    print("服务器已启动")
    print(f"请在浏览器中访问: http://{local_ip}:5933")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5933, debug=False)
