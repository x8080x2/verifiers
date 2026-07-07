from flask import Flask, request, Response, stream_with_context, jsonify
import requests
import json
import time
import os
import io
import csv
import re
import logging
import secrets
import zipfile

from db import (init_schema, store_file, get_file, create_job, get_job,
                update_job_progress, complete_job, fail_job, cleanup_old_jobs,
                count_active_jobs_for_user, count_all_active_jobs,
                get_oldest_queued_job, queue_job, get_conn,
                get_balance, add_credits, create_payment_request,
                get_payment_request, approve_payment, deny_payment)

# --- Configuration ---
def load_dotenv_file(dotenv_path):
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        return


load_dotenv_file(os.path.join(os.path.dirname(__file__), ".env"))

APP_NAME = "ClosedVerifier"
APP_VERSION = os.getenv("APP_VERSION", "0.1.0")
START_TIME = time.time()

API_KEY = os.getenv("DEBOUNCE_API_KEY", "").strip()
API_CONNECT_TIMEOUT_S = float(os.getenv("DEBOUNCE_CONNECT_TIMEOUT_S", "3.05"))
API_READ_TIMEOUT_S = float(os.getenv("DEBOUNCE_READ_TIMEOUT_S", "10"))

BULK_BASE_URL = "https://bulk.debounce.io/v1/"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
BULK_MAX_EMAILS = int(os.getenv("BULK_MAX_EMAILS", "200000"))
BULK_MAX_FILE_BYTES = int(os.getenv("BULK_MAX_FILE_BYTES", str(20 * 1024 * 1024)))
BULK_FILE_TTL_S = int(os.getenv("BULK_FILE_TTL_S", "86400"))

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))

# Wallet addresses for top-up
BTC_WALLET = os.getenv("BTC_WALLET", "")
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "")
USDT_ERC20_WALLET = os.getenv("USDT_ERC20_WALLET", "")

# Telegram admin bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Common Free/Personal Email Providers
FREE_PROVIDERS = {
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com', 'icloud.com', 
    'mail.com', 'protonmail.com', 'zoho.com', 'yandex.com', 'live.com', 'msn.com',
    'me.com', 'mac.com', 'comcast.net', 'sbcglobal.net', 'verizon.net', 'att.net'
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Schema initialized on first request via before_first_request equivalent

# --- Frontend HTML (Embedded) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ClosedEmailDeBounce | Enterprise Email Validator</title>
    
    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    
    <!-- Font Awesome -->
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    
    <!-- Google Fonts -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">

    <script>
        window.USER_ID = "__USER_ID__";

        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: {
                        sans: ['Inter', 'sans-serif'],
                    },
                    colors: {
                        brand: {
                            50: '#f0f9ff',
                            100: '#e0f2fe',
                            500: '#0ea5e9',
                            600: '#0284c7',
                            900: '#0c4a6e',
                        }
                    }
                }
            }
        }
    </script>

    <style>
        body { font-family: 'Inter', sans-serif; background-color: #f8fafc; font-size: 0.875rem; }
        .glass-panel {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 10px 15px -3px rgba(0, 0, 0, 0.05);
        }
        .animate-pulse-slow { animation: pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
        .custom-scrollbar::-webkit-scrollbar { width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: #f1f5f9; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
    </style>
</head>
<body class="text-slate-800 antialiased min-h-screen flex flex-col">

    <!-- Navbar -->
    <nav class="bg-white border-b border-slate-200 sticky top-0 z-50 h-10">
        <div class="max-w-6xl mx-auto px-4 h-full">
            <div class="flex justify-between items-center h-full">
                <div class="flex items-center gap-2">
                    <div class="bg-brand-600 text-white p-1 rounded shadow-sm">
                        <i class="fas fa-shield-alt text-xs"></i>
                    </div>
                    <div>
                        <h1 class="text-xs font-bold text-slate-900 tracking-tight leading-none">ClosedVerifier</h1>
                    </div>
                </div>
                <div class="flex items-center gap-3">
                    <div id="balance-display" class="hidden md:flex items-center gap-1.5 px-2 py-0.5 bg-amber-50 text-amber-800 rounded-full text-[9px] font-semibold border border-amber-200 cursor-pointer hover:bg-amber-100 transition-colors" onclick="openTopup()">
                        <i class="fas fa-coins text-[9px]"></i>
                        <span>$<span id="balance-amount">0.00</span></span>
                    </div>
                    <div id="status-badge" class="hidden md:flex items-center gap-1.5 px-2 py-0.5 bg-slate-100 text-slate-500 rounded-full text-[9px] font-semibold border border-slate-200">
                        <span class="w-1.5 h-1.5 bg-slate-400 rounded-full"></span>
                        Checking API...
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Stats Bar (inline pills) -->
    <div class="container mx-auto max-w-6xl" style="padding: 0 16px;">
        <div class="bg-white rounded-lg px-3 py-2 shadow-sm mb-3 flex items-center justify-between">
            <div class="flex gap-3 text-[10px] font-semibold">
                <span class="text-slate-400">Total <span class="text-slate-800 ml-1" id="stat-total">0</span></span>
                <span class="text-green-500">Valid <span class="text-green-700 ml-1" id="stat-valid">0</span></span>
                <span class="text-red-500">Invalid <span class="text-red-700 ml-1" id="stat-invalid">0</span></span>
                <span class="text-indigo-400">Filtered <span class="text-indigo-700 ml-1" id="stat-filtered">0</span></span>
            </div>
            <span class="text-[8px] text-slate-400"><i class="far fa-clock"></i> Ready</span>
        </div>
    </div>

    <!-- Main Content -->
    <main class="flex-grow container mx-auto max-w-6xl py-3">
        
        <div class="flex gap-3 px-4">
            
            <!-- LEFT COLUMN: INPUT -->
            <div class="lg:w-4/12 flex flex-col gap-2">
                
                <!-- Input Panel -->
                <div class="rounded-lg p-3 flex flex-col bg-white border border-slate-200 shadow-sm" id="inputZone">
                    <div class="flex items-center justify-between mb-2">
                        <h3 class="text-[11px] font-semibold text-slate-700">
                            <i class="fas fa-pen-to-square text-brand-600 mr-1.5"></i>Input Emails
                        </h3>
                        <label class="flex items-center gap-1 text-[9px] text-slate-500 cursor-pointer select-none">
                            <input type="checkbox" id="b2bCheck" class="w-3 h-3 rounded border-slate-300 text-brand-600">
                            B2B Only
                        </label>
                    </div>
                    
                    <!-- Text Input Area -->
                    <textarea id="emailText" class="w-full h-20 px-2.5 py-1.5 text-xs text-slate-700 border border-slate-200 rounded-lg focus:ring-1 focus:ring-brand-400 focus:border-transparent custom-scrollbar resize-none font-mono mb-2" placeholder="paste@emails.here&#10;one@per.line"></textarea>

                    <!-- Actions Row -->
                    <input type="file" id="fileInput" accept=".txt,.csv" class="hidden">
                    <div class="flex gap-2">
                        <button id="validateBtn" onclick="startValidation()" class="flex-1 py-1.5 bg-brand-600 hover:bg-brand-700 text-white text-[10px] font-medium rounded-lg transition-colors flex items-center justify-center gap-1 shadow-sm">
                            <i class="fas fa-check-circle text-[9px]"></i> Validate
                        </button>
                        <button onclick="document.getElementById('fileInput').click()" class="px-2.5 py-1.5 border border-dashed border-slate-300 text-slate-400 text-[10px] rounded-lg hover:border-brand-300 hover:text-brand-500 transition-colors" title="Upload file">
                            <i class="fas fa-file-upload"></i>
                        </button>
                    </div>
                    <div class="mt-1 text-right text-[8px] text-slate-400">
                        <span id="fileNameLabel" class="truncate inline-block max-w-full">Select .txt or .csv file</span>
                    </div>
                </div>
            </div>

            <!-- RIGHT COLUMN: RESULTS -->
            <div class="lg:w-8/12 flex flex-col gap-2">
                
                <!-- Results Dashboard -->
                <div id="results-panel" class="rounded-lg border border-slate-200 shadow-sm bg-white flex flex-col min-h-[200px]">
                    
                    <!-- Header -->
                    <div class="flex items-center justify-between px-3 py-2 border-b border-slate-100">
                        <div class="flex items-center gap-2">
                            <div class="relative w-8 h-8 flex items-center justify-center">
                                <svg class="transform -rotate-90 w-10 h-10 flex-shrink-0">
                                    <circle cx="20" cy="20" r="16" stroke="currentColor" stroke-width="4" fill="transparent" class="text-slate-100" />
                                    <circle id="progress-ring" cx="20" cy="20" r="16" stroke="currentColor" stroke-width="4" fill="transparent" class="text-brand-600 transition-all duration-500" stroke-dasharray="100.5" stroke-dashoffset="100.5" />
                                </svg>
                                <span class="absolute text-[8px] font-bold text-slate-600" id="progress-text">0%</span>
                            </div>
                            <div>
                                <p class="text-xs font-semibold text-slate-800" id="status-title">Ready to Start</p>
                                <p class="text-[9px] text-slate-400" id="status-desc">Waiting for input...</p>
                            </div>
                        </div>
                        <div class="flex gap-1.5">
                            <button id="download-btn" disabled class="px-2 py-0.5 bg-slate-100 text-slate-400 text-[9px] font-medium rounded cursor-not-allowed flex items-center gap-1">
                                <i class="fas fa-download"></i> Download Valid
                            </button>
                            <button onclick="resetUI()" class="px-2 py-0.5 text-slate-500 hover:text-slate-700 text-[9px] font-medium rounded border border-transparent hover:border-slate-200">
                                Reset
                            </button>
                        </div>
                    </div>

                    <!-- Log -->
                    <div class="flex-1 flex flex-col min-h-[140px]">
                        <div class="flex items-center justify-between px-3 pt-1.5 pb-0.5">
                            <h4 class="text-[9px] font-semibold text-slate-500 flex items-center gap-1.5">
                                <i class="fas fa-list-ul"></i> Activity Log
                                <span class="flex h-1.5 w-1.5" id="live-indicator" style="opacity: 0;">
                                  <span class="animate-ping absolute inline-flex h-1.5 w-1.5 rounded-full bg-green-400 opacity-75"></span>
                                  <span class="relative inline-flex rounded-full h-1.5 w-1.5 bg-green-500"></span>
                                </span>
                            </h4>
                            <button id="copy-btn" onclick="copyValidEmails()" class="text-[8px] text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1 opacity-50 cursor-not-allowed transition-all" disabled>
                                <i class="fas fa-copy"></i> Copy
                            </button>
                        </div>
                        <div class="bg-slate-900 rounded-lg shadow-inner flex-1 mx-2 mb-2 overflow-hidden border border-slate-800">
                            <div class="h-full overflow-y-auto p-2 font-mono text-[10px] custom-scrollbar space-y-0.5" id="console-output">
                                <div class="text-slate-600 italic text-center mt-6">Results will appear here...</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

        </div>
    </main>

    <!-- Top-Up Modal -->
    <div id="topup-modal" class="fixed inset-0 bg-black/60 flex items-center justify-center z-50 hidden">
        <div class="bg-white rounded-xl shadow-2xl max-w-lg w-full mx-4 overflow-hidden border border-slate-200">
            <!-- Header -->
            <div class="flex items-center justify-between px-4 py-3 border-b border-slate-100">
                <h3 class="text-sm font-bold text-slate-800"><i class="fas fa-coins text-amber-500 mr-2"></i>Top Up Balance</h3>
                <button onclick="closeTopup()" class="text-slate-400 hover:text-slate-600 transition-colors"><i class="fas fa-times"></i></button>
            </div>
            <div class="p-4 space-y-4">
                <!-- Step 1: Enter Amount -->
                <div id="topup-step-1">
                    <label class="text-[11px] font-semibold text-slate-600 block mb-2">Enter amount (USD):</label>
                    <div class="flex gap-2">
                        <input id="topup-amount" type="number" min="1" step="1" value="50"
                               class="flex-1 px-3 py-2 text-sm border border-slate-300 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-transparent outline-none"
                               oninput="updateBtcEstimate()">
                        <button onclick="generateWallets()" class="px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-xs font-semibold rounded-lg transition-colors shadow-sm">
                            Generate
                        </button>
                    </div>
                    <p class="text-[9px] text-slate-400 mt-1">≈ <span id="btc-estimate">0.0000</span> BTC</p>
                </div>

                <!-- Step 2: Wallet Addresses -->
                <div id="topup-step-2" class="hidden">
                    <div class="bg-red-50 border border-red-200 rounded-lg p-3 mb-3">
                        <p class="text-[10px] font-bold text-red-700 text-center">
                            SEND $<span id="topup-amount-display">50</span> TO ANY WALLET BELOW:
                        </p>
                    </div>
                    <div class="space-y-2">
                        <div class="bg-slate-50 rounded-lg p-2.5 border border-slate-200">
                            <p class="text-[9px] font-semibold text-slate-500 mb-1">BTC:</p>
                            <div class="flex gap-1">
                                <input id="wallet-btc" readonly
                                       class="flex-1 px-2 py-1 text-[10px] font-mono bg-white border border-slate-200 rounded text-slate-700 truncate outline-none"
                                       onclick="this.select()">
                                <button onclick="copyWallet('btc')" class="px-2 py-1 bg-slate-200 hover:bg-slate-300 text-slate-600 text-[9px] rounded transition-colors"><i class="fas fa-copy"></i></button>
                            </div>
                        </div>
                        <div class="bg-slate-50 rounded-lg p-2.5 border border-slate-200">
                            <p class="text-[9px] font-semibold text-slate-500 mb-1">USDT TRC20:</p>
                            <div class="flex gap-1">
                                <input id="wallet-usdt-trc20" readonly
                                       class="flex-1 px-2 py-1 text-[10px] font-mono bg-white border border-slate-200 rounded text-slate-700 truncate outline-none"
                                       onclick="this.select()">
                                <button onclick="copyWallet('usdt_trc20')" class="px-2 py-1 bg-slate-200 hover:bg-slate-300 text-slate-600 text-[9px] rounded transition-colors"><i class="fas fa-copy"></i></button>
                            </div>
                        </div>
                        <div class="bg-slate-50 rounded-lg p-2.5 border border-slate-200">
                            <p class="text-[9px] font-semibold text-slate-500 mb-1">USDT ERC20:</p>
                            <div class="flex gap-1">
                                <input id="wallet-usdt-erc20" readonly
                                       class="flex-1 px-2 py-1 text-[10px] font-mono bg-white border border-slate-200 rounded text-slate-700 truncate outline-none"
                                       onclick="this.select()">
                                <button onclick="copyWallet('usdt_erc20')" class="px-2 py-1 bg-slate-200 hover:bg-slate-300 text-slate-600 text-[9px] rounded transition-colors"><i class="fas fa-copy"></i></button>
                            </div>
                        </div>
                    </div>

                    <!-- Auto-update + No refund notice -->
                    <div class="bg-green-50 border border-green-200 rounded-lg p-2 mt-3">
                        <p class="text-[9px] font-semibold text-green-700 text-center">✅ BALANCE WILL BE AUTO-UPDATED IF PAYMENT CONFIRMED</p>
                    </div>
                    <div class="bg-red-50 border border-red-200 rounded-lg p-2 mt-1">
                        <p class="text-[9px] font-bold text-red-600 text-center">‼️ NO REFUND POLICY</p>
                    </div>

                    <!-- I've Paid button -->
                    <button id="paid-btn" onclick="notifyPaid()" class="w-full py-2.5 mt-3 bg-green-600 hover:bg-green-700 text-white text-xs font-bold rounded-lg transition-all shadow-sm flex items-center justify-center gap-2">
                        <i class="fas fa-check-circle"></i> I'VE PAID
                    </button>
                    <p id="paid-status" class="text-[9px] text-slate-400 text-center mt-1 hidden">Notification sent. Admin will verify your payment.</p>
                </div>
            </div>
        </div>
    </div>

    <footer class="mt-auto py-3 border-t border-slate-200 bg-white">
        <div class="container mx-auto px-4 text-center text-slate-400 text-[9px]">
            <p>&copy; 2026 ClosedVerifier.</p>
        </div>
    </footer>

    <!-- LOGIC -->
    <script>
        // DOM Elements
        const fileInput = document.getElementById('fileInput');
        const emailText = document.getElementById('emailText');
        const fileNameLabel = document.getElementById('fileNameLabel');
        const validateBtn = document.getElementById('validateBtn');
        const consoleOutput = document.getElementById('console-output');
        const liveIndicator = document.getElementById('live-indicator');
        const statusBadge = document.getElementById('status-badge');
        const b2bCheck = document.getElementById('b2bCheck');
        const copyBtn = document.getElementById('copy-btn');
        
        // Stats Elements
        const elTotal = document.getElementById('stat-total');
        const elValid = document.getElementById('stat-valid');
        const elInvalid = document.getElementById('stat-invalid');
        const elFiltered = document.getElementById('stat-filtered');
        
        // Progress Elements
        const progressRing = document.getElementById('progress-ring');
        const progressText = document.getElementById('progress-text');
        const statusTitle = document.getElementById('status-title');
        const statusDesc = document.getElementById('status-desc');
        const downloadBtn = document.getElementById('download-btn');

        // State
        let stats = { total: 0, valid: 0, invalid: 0, filtered: 0, processed: 0 };
        let deliverableEmails = [];
        let selectedFile = null;

        // --- Initialization ---
        window.addEventListener('load', () => {
            checkApiStatus();
            loadBalance();
        });

        // ── Balance / Top-up Functions ──

        let topupReqId = null;
        let btcPrice = 0;

        async function loadBalance() {
            try {
                const res = await fetch(`/api/balance?uid=${encodeURIComponent(window.USER_ID)}`);
                const data = await res.json();
                document.getElementById('balance-amount').textContent = data.balance.toFixed(2);
            } catch (e) { /* ignore */ }
        }

        function openTopup() {
            document.getElementById('topup-modal').classList.remove('hidden');
            document.getElementById('topup-step-1').classList.remove('hidden');
            document.getElementById('topup-step-2').classList.add('hidden');
            document.getElementById('paid-status').classList.add('hidden');
            document.getElementById('topup-amount').value = 50;
            topupReqId = null;
            updateBtcEstimate();
        }

        function closeTopup() {
            document.getElementById('topup-modal').classList.add('hidden');
        }

        async function updateBtcEstimate() {
            if (!btcPrice) {
                try {
                    const r = await fetch('/api/wallets');
                    const d = await r.json();
                    btcPrice = d.btc_price || 0;
                } catch (e) { return; }
            }
            const usd = parseFloat(document.getElementById('topup-amount').value) || 0;
            const btc = btcPrice > 0 ? (usd / btcPrice) : 0;
            document.getElementById('btc-estimate').textContent = btc.toFixed(8);
        }

        async function generateWallets() {
            const amount = parseFloat(document.getElementById('topup-amount').value) || 0;
            if (amount < 1) { alert('Minimum $1'); return; }

            try {
                // Fetch wallets
                const wres = await fetch('/api/wallets');
                const wallets = await wres.json();
                btcPrice = wallets.btc_price || 0;

                document.getElementById('wallet-btc').value = wallets.btc || 'Not configured';
                document.getElementById('wallet-usdt-trc20').value = wallets.usdt_trc20 || 'Not configured';
                document.getElementById('wallet-usdt-erc20').value = wallets.usdt_erc20 || 'Not configured';

                // Create payment request
                const pres = await fetch('/api/topup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({amount, uid: window.USER_ID}),
                });
                const pdata = await pres.json();
                topupReqId = pdata.id;

                // Show step 2
                document.getElementById('topup-amount-display').textContent = amount;
                document.getElementById('topup-step-1').classList.add('hidden');
                document.getElementById('topup-step-2').classList.remove('hidden');
            } catch (e) {
                alert('Failed to generate. Try again.');
            }
        }

        async function copyWallet(name) {
            const input = document.getElementById(`wallet-${name}`);
            if (input && input.value) {
                try {
                    await navigator.clipboard.writeText(input.value);
                } catch (e) {
                    input.select();
                    document.execCommand('copy');
                }
            }
        }

        async function notifyPaid() {
            if (!topupReqId) return;
            document.getElementById('paid-btn').disabled = true;
            document.getElementById('paid-btn').innerHTML = '<i class="fas fa-spinner fa-spin"></i> Sending...';
            try {
                await fetch('/api/topup/notify', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: topupReqId, uid: window.USER_ID}),
                });
                document.getElementById('paid-status').classList.remove('hidden');
                document.getElementById('paid-btn').innerHTML = '<i class="fas fa-check-circle"></i> NOTIFIED ✓';
            } catch (e) {
                document.getElementById('paid-btn').disabled = false;
                document.getElementById('paid-btn').innerHTML = '<i class="fas fa-check-circle"></i> I\\'VE PAID';
            }
        }

        async function checkApiStatus() {
            try {
                const res = await fetch('/health');
                const data = await res.json();

                if (!res.ok || data.status !== 'ok') {
                    const reason = (data.reason || data.error || `HTTP ${res.status}`).toString();
                    statusBadge.innerHTML = `<span class="w-1.5 h-1.5 bg-red-500 rounded-full"></span> ${reason}`;
                    statusBadge.className = "hidden md:flex items-center gap-1.5 px-2 py-0.5 bg-red-50 text-red-700 rounded-full text-[10px] font-semibold border border-red-100";
                    return;
                }

                if (!data.api_key_set) {
                    statusBadge.innerHTML = '<span class="w-1.5 h-1.5 bg-red-500 rounded-full"></span> Missing API Key';
                    statusBadge.className = "hidden md:flex items-center gap-1.5 px-2 py-0.5 bg-red-50 text-red-700 rounded-full text-[10px] font-semibold border border-red-100";
                    return;
                }

                if (!data.bulk_ready) {
                    statusBadge.innerHTML = '<span class="w-1.5 h-1.5 bg-amber-500 rounded-full animate-pulse"></span> Bulk Needs HTTPS URL';
                    statusBadge.className = "hidden md:flex items-center gap-1.5 px-2 py-0.5 bg-amber-50 text-amber-800 rounded-full text-[10px] font-semibold border border-amber-100";
                    statusBadge.title = "Set PUBLIC_BASE_URL to an https URL reachable by DeBounce";
                    return;
                }

                statusBadge.innerHTML = '<span class="w-1.5 h-1.5 bg-green-500 rounded-full animate-pulse"></span> Ready';
                statusBadge.className = "hidden md:flex items-center gap-1.5 px-2 py-0.5 bg-green-50 text-green-700 rounded-full text-[10px] font-semibold border border-green-100";
            } catch (e) {
                statusBadge.innerHTML = '<span class="w-1.5 h-1.5 bg-red-500 rounded-full"></span> API Offline';
                statusBadge.className = "hidden md:flex items-center gap-1.5 px-2 py-0.5 bg-red-50 text-red-700 rounded-full text-[10px] font-semibold border border-red-100";
            }
        }

        // --- Input Handling ---
        // Ensure Cmd/Ctrl+A selects all text in textarea
        emailText.addEventListener('keydown', (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'a') {
                e.target.select();
                e.preventDefault();
            }
        });

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length) {
                selectedFile = e.target.files[0];
                fileNameLabel.textContent = selectedFile.name;
                fileNameLabel.classList.add('text-brand-600', 'font-semibold');
                
                emailText.value = ''; // Clear text
                emailText.disabled = true;
                emailText.classList.add('bg-slate-50', 'text-slate-400');
                emailText.placeholder = `File selected: ${selectedFile.name}`;
            }
        });
        
        // --- Copy Functionality ---
        async function copyValidEmails() {
            if (deliverableEmails.length === 0) return;
            
            try {
                await navigator.clipboard.writeText(deliverableEmails.join('\\n'));
                
                const originalText = copyBtn.innerHTML;
                copyBtn.innerHTML = '<i class="fas fa-check"></i> Copied!';
                copyBtn.classList.add('text-green-600');
                
                setTimeout(() => {
                    copyBtn.innerHTML = originalText;
                    copyBtn.classList.remove('text-green-600');
                }, 2000);
            } catch (err) {
                console.error('Failed to copy: ', err);
            }
        }

        function resetUI() {
            // Reset Inputs
            emailText.value = '';
            emailText.disabled = false;
            emailText.classList.remove('bg-slate-50', 'text-slate-400');
            emailText.placeholder = "paste@emails.here\\none@per.line";
            
            fileInput.value = '';
            selectedFile = null;
            fileNameLabel.textContent = "Select .txt or .csv file";
            fileNameLabel.classList.remove('text-brand-600', 'font-semibold');
            
            validateBtn.disabled = false;
            validateBtn.innerHTML = '<i class="fas fa-check-circle"></i> Validate Now';
            validateBtn.classList.remove('opacity-50', 'cursor-not-allowed');

            // Reset Stats
            stats = { total: 0, valid: 0, invalid: 0, filtered: 0, processed: 0 };
            deliverableEmails = [];
            
            elTotal.innerText = '0';
            elValid.innerText = '0';
            elInvalid.innerText = '0';
            elFiltered.innerText = '0';
            
            progressRing.style.strokeDashoffset = 100.5;
            progressText.innerText = '0%';
            statusTitle.innerText = 'Ready to Start';
            statusDesc.innerText = 'Waiting for input...';
            
            downloadBtn.disabled = true;
            downloadBtn.classList.add('bg-slate-100', 'text-slate-400', 'cursor-not-allowed');
            downloadBtn.classList.remove('bg-brand-600', 'text-white', 'hover:bg-brand-700', 'shadow-sm');
            
            copyBtn.disabled = true;
            copyBtn.classList.add('opacity-50', 'cursor-not-allowed');
            
            consoleOutput.innerHTML = '<div class="text-slate-600 italic text-center mt-10">Results will appear here...</div>';
        }

        async function startValidation() {
            const textContent = emailText.value.trim();
            
            if (!selectedFile && !textContent) {
                alert("Please paste emails or upload a file first.");
                return;
            }

            // Lock UI
            validateBtn.disabled = true;
            validateBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Processing...';
            validateBtn.classList.add('opacity-50', 'cursor-not-allowed');
            
            statusTitle.innerText = "Processing...";
            statusDesc.innerText = selectedFile ? selectedFile.name : "Text Input";
            consoleOutput.innerHTML = ''; // Clear log
            liveIndicator.style.opacity = 1;

            const formData = new FormData();
            
            if (selectedFile) {
                formData.append('file', selectedFile);
            } else {
                const blob = new Blob([emailText.value], { type: 'text/plain' });
                formData.append('file', blob, 'pasted_emails.txt');
            }
            
            // Add B2B Checkbox state
            if (b2bCheck.checked) {
                formData.append('filter_free', 'true');
            }
            
            // Add user ID for job isolation
            formData.append('uid', window.USER_ID);

            try {
                const response = await fetch('/validate', { method: 'POST', body: formData });
                const contentType = response.headers.get('content-type') || '';

                if (contentType.includes('application/x-ndjson')) {
                    await streamNdjsonResponse(response);
                    return;
                }

                const data = await response.json();
                if (!response.ok) {
                    consoleOutput.innerHTML += `<div class="text-red-500 p-2">Request Error: ${data.error || response.status}</div>`;
                    finishValidation();
                    return;
                }

                if (data.mode === 'bulk') {
                    await runBulkJob(data);
                    return;
                }

                if (data.mode === 'queued') {
                    consoleOutput.innerHTML += `<div class="text-yellow-400 p-2">${data.message || 'Job queued — will process when DeBounce is free.'}</div>`;
                    statusTitle.innerText = "Queued";
                    statusDesc.innerText = `${data.submitted_total} emails waiting`;
                    finishValidation();
                    return;
                }

                consoleOutput.innerHTML += `<div class="text-red-500 p-2">Unsupported response mode.</div>`;
                finishValidation();
            } catch (err) {
                consoleOutput.innerHTML += `<div class="text-red-500 p-2">System Error: ${err.message}</div>`;
                finishValidation(); // Unlock UI on error
            }
        }

        async function runBulkJob(job) {
            stats.total = job.submitted_total || 0;
            stats.processed = 0;
            elTotal.innerText = stats.total;

            statusTitle.innerText = "Queued...";
            statusDesc.innerText = `List ${job.list_id}`;

            await pollBulk(job.list_id);
            await streamNdjsonResponse(await fetch(`/bulk/results_stream?list_id=${encodeURIComponent(job.list_id)}`));
        }

        async function pollBulk(listId) {
            while (true) {
                const res = await fetch(`/bulk/status?list_id=${encodeURIComponent(listId)}`);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(data.error || 'Bulk status error');
                }

                const percent = Math.max(0, Math.min(100, data.percentage || 0));
                const processed = Math.min(stats.total, Math.floor((percent / 100) * stats.total));

                stats.processed = processed;
                elTotal.innerText = stats.total;

                const circumference = 100.5;
                const offset = circumference - (percent / 100) * circumference;
                progressRing.style.strokeDashoffset = offset;
                progressText.innerText = `${percent}%`;

                statusTitle.innerText = (data.status || 'processing').toString().toUpperCase();
                statusDesc.innerText = `${processed}/${stats.total} processed`;

                if ((data.status || '').toString().toLowerCase() === 'completed') {
                    break;
                }

                await new Promise(r => setTimeout(r, 1500));
            }
        }

        async function streamNdjsonResponse(response) {
            if (!response.ok) {
                const text = await response.text();
                consoleOutput.innerHTML += `<div class="text-red-500 p-2">Request Error: ${text || response.status}</div>`;
                finishValidation();
                return;
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\\n');
                buffer = lines.pop();

                for (const line of lines) {
                    if (!line.trim()) continue;
                    try {
                        const data = JSON.parse(line);
                        if (data.type === 'progress') updateProgress(data);
                        if (data.type === 'complete') finishValidation();
                    } catch (e) { console.error(e); }
                }
            }
        }

        function updateProgress(data) {
            // Update Counts
            stats.processed = data.current;
            stats.total = data.total;
            
            if (data.status === 'valid') {
                stats.valid++;
                deliverableEmails.push(data.email);
            } else if (data.status === 'invalid') {
                stats.invalid++;
            } else if (data.status === 'filtered') {
                stats.filtered++;
            } else {
                if (typeof stats.risky === 'undefined') stats.risky = 0;
                stats.risky++;
            }

            // Update DOM
            elTotal.innerText = stats.total;
            elValid.innerText = stats.valid;
            elInvalid.innerText = stats.invalid;
            elFiltered.innerText = stats.filtered;

            // Update Progress Ring
            const percent = Math.round((stats.processed / stats.total) * 100);
            const circumference = 100.5;
            const offset = circumference - (percent / 100) * circumference;
            progressRing.style.strokeDashoffset = offset;
            progressText.innerText = `${percent}%`;

            // Enable copy button if we have valid emails
            if (deliverableEmails.length > 0 && copyBtn.disabled) {
                copyBtn.disabled = false;
                copyBtn.classList.remove('opacity-50', 'cursor-not-allowed');
            }

            // Add Log Entry
            let statusColor = 'text-gray-400';
            let icon = '-';
            
            if (data.status === 'valid') {
                statusColor = 'text-green-400';
                icon = '✓';
            } else if (data.status === 'invalid') {
                statusColor = 'text-red-400';
                icon = '✗';
            } else if (data.status === 'filtered') {
                statusColor = 'text-blue-400';
                icon = 'Filter';
            } else {
                statusColor = 'text-yellow-400';
                icon = '!';
            }
            
            const logItem = document.createElement('div');
            logItem.className = 'flex justify-between items-center py-1 border-b border-slate-800 hover:bg-slate-800 px-2 transition-colors group';
            logItem.innerHTML = `
                <span class="text-slate-300 truncate w-2/3 font-mono text-[11px] group-hover:text-white">${data.email}</span>
                <span class="${statusColor} text-[10px] font-bold uppercase flex items-center gap-1">
                    ${data.reason || data.status} <span class="w-3 text-center">${icon}</span>
                </span>
            `;
            consoleOutput.appendChild(logItem);
            consoleOutput.scrollTop = consoleOutput.scrollHeight;
        }

        function finishValidation() {
            statusTitle.innerText = "Complete";
            statusDesc.innerText = `${stats.total} processed`;
            progressRing.classList.add('text-green-500');
            progressRing.classList.remove('text-brand-600');
            liveIndicator.style.opacity = 0;
            
            validateBtn.innerHTML = '<i class="fas fa-check-circle"></i> Validate Again';
            validateBtn.disabled = false;
            validateBtn.classList.remove('opacity-50', 'cursor-not-allowed');

            // Enable Download only if there are valid emails
            if (deliverableEmails.length > 0) {
                downloadBtn.disabled = false;
                downloadBtn.classList.remove('bg-slate-100', 'text-slate-400', 'cursor-not-allowed');
                downloadBtn.classList.add('bg-brand-600', 'text-white', 'hover:bg-brand-700', 'shadow-sm');
                
                downloadBtn.onclick = () => {
                    const blob = new Blob([deliverableEmails.join('\\n')], { type: 'text/plain' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `deliverable_emails_${new Date().toISOString().slice(0,10)}.txt`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                };
            }
        }
    </script>
</body>
</html>
"""

# --- Backend Logic ---

def uptime_seconds():
    return max(0, int(time.time() - START_TIME))


def has_api_key():
    return bool(API_KEY)


def normalize_email(raw):
    if raw is None:
        return ""
    s = str(raw).strip().strip("<>").strip()
    return s.lower()


def is_plausible_email(email):
    if not email:
        return False
    if " " in email:
        return False
    return bool(EMAIL_RE.fullmatch(email))


def extract_emails_from_text(content):
    if not content:
        return []
    return [normalize_email(m.group(0)) for m in EMAIL_RE.finditer(content)]


def extract_emails_from_csv(content):
    if not content:
        return []

    sample = content[:4096]
    dialect = None
    try:
        dialect = csv.Sniffer().sniff(sample)
    except Exception:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(content, newline=None), dialect)
    first_row = None
    for row in reader:
        if row:
            first_row = row
            break

    if not first_row:
        return []

    header = [str(c).strip().lower() for c in first_row]
    email_col = None
    for idx, name in enumerate(header):
        if "email" in name or "e-mail" in name or "mail" == name:
            email_col = idx
            break

    emails = []
    if email_col is not None:
        for row in reader:
            if email_col < len(row):
                emails.extend(extract_emails_from_text(row[email_col]))
        if emails:
            return emails

    emails.extend(extract_emails_from_text(",".join(first_row)))
    for row in reader:
        for cell in row:
            if "@" in cell:
                emails.extend(extract_emails_from_text(cell))
                break
    if emails:
        return emails

    return extract_emails_from_text(content)


def extract_emails(content, filename):
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return extract_emails_from_csv(content)
    return extract_emails_from_text(content)


def public_url(path):
    if not PUBLIC_BASE_URL:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return PUBLIC_BASE_URL + path


def bulk_store_file(filename, content_bytes, user_id=1):
    cleanup_old_jobs(max_age_s=BULK_FILE_TTL_S)
    return store_file(filename, content_bytes, user_id=user_id)


def bulk_api_upload(file_url):
    if not has_api_key():
        return None, "Missing DEBOUNCE_API_KEY"
    if not PUBLIC_BASE_URL or not PUBLIC_BASE_URL.startswith("https://"):
        return None, "PUBLIC_BASE_URL must be an https URL reachable by DeBounce"
    if not file_url.startswith("https://"):
        return None, "File URL must start with https://"

    try:
        res = requests.get(
            BULK_BASE_URL + "upload/",
            params={"api": API_KEY, "url": file_url},
            timeout=(API_CONNECT_TIMEOUT_S, API_READ_TIMEOUT_S),
        )
        data = res.json()
    except Exception:
        logger.exception("Bulk upload failed")
        return None, "Bulk upload failed"

    if data.get("success") != "1":
        debounce = data.get("debounce") or {}
        return None, debounce.get("error") or "Bulk upload failed"

    debounce = data.get("debounce") or {}
    list_id = str(debounce.get("list_id") or "").strip()
    if not list_id:
        return None, "Bulk upload returned no list_id"
    return list_id, None


def process_queue():
    """Process the next queued job if DeBounce is free."""
    try:
        if count_all_active_jobs() > 1:  # only queued jobs remain
            return

        queued = get_oldest_queued_job()
        if not queued:
            return

        logger.info("Processing queued job list_id=%s", queued["list_id"])

        # Get file and construct URL
        f = get_file(queued["token"])
        if not f:
            fail_job(queued["list_id"])
            return

        file_url = public_url(f"/bulk/file/{queued['token']}.txt")
        list_id, err = bulk_api_upload(file_url)
        if err:
            if "maximum number" not in err.lower() and "existing request" not in err.lower():
                fail_job(queued["list_id"])
            return

        # Update the queued job with real list_id and set to processing
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE validation_jobs SET list_id = %s, status = 'processing', updated_at = now() WHERE id = %s",
                    (list_id, queued["id"]),
                )
            conn.commit()
        logger.info("Queued job %d now processing with list_id=%s", queued["id"], list_id)
    except Exception:
        logger.exception("process_queue failed")


def bulk_api_status(list_id):
    if not has_api_key():
        return None, "Missing DEBOUNCE_API_KEY"
    if not list_id:
        return None, "Missing list_id"
    try:
        res = requests.get(
            BULK_BASE_URL + "status/",
            params={"api": API_KEY, "list_id": list_id},
            timeout=(API_CONNECT_TIMEOUT_S, API_READ_TIMEOUT_S),
        )
        data = res.json()
    except Exception:
        logger.exception("Bulk status failed")
        return None, "Bulk status failed"
    if data.get("success") != "1":
        debounce = data.get("debounce") or {}
        return None, debounce.get("error") or "Bulk status failed"
    debounce = data.get("debounce") or {}
    out = {
        "list_id": str(debounce.get("list_id") or list_id),
        "status": str(debounce.get("status") or "").lower(),
        "percentage": int(debounce.get("percentage") or 0),
        "download_link": str(debounce.get("download_link") or ""),
    }
    return out, None

@app.route('/')
def index():
    uid = request.args.get("uid", "anonymous")
    template = HTML_TEMPLATE.replace("__USER_ID__", uid)
    return template


@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.route('/health')
def health():
    bulk_ready = bool(PUBLIC_BASE_URL) and PUBLIC_BASE_URL.startswith("https://")
    return jsonify(
        {
            "status": "ok",
            "app": APP_NAME,
            "version": APP_VERSION,
            "uptime_s": uptime_seconds(),
            "api_key_set": has_api_key(),
            "bulk_ready": bulk_ready,
        }
    )


# ── Balance / Top-up API ──

def _fetch_btc_price():
    """Get live BTC price in USD."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                         timeout=(3, 5))
        return r.json().get("bitcoin", {}).get("usd", 0)
    except Exception:
        return 0


def _notify_admin(message: str):
    """Send a Telegram notification to the admin."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=(3, 5),
        )
    except Exception:
        logger.exception("Failed to send Telegram notification")


@app.route("/api/wallets")
def api_wallets():
    """Return wallet addresses and live BTC price."""
    btc_price = _fetch_btc_price()
    return jsonify({
        "btc_price": btc_price,
        "btc": BTC_WALLET,
        "usdt_trc20": USDT_TRC20_WALLET,
        "usdt_erc20": USDT_ERC20_WALLET,
    })


@app.route("/api/balance")
def api_balance():
    """Get user balance by uid."""
    uid_str = request.args.get("uid", "anonymous").strip()
    uid_int = (abs(hash(uid_str)) % (2**31 - 1)) or 1
    bal = get_balance(uid_int)
    return jsonify({"balance": bal, "uid": uid_str})


@app.route("/api/topup", methods=["POST"])
def api_topup():
    """Create a payment request."""
    data = request.get_json(silent=True) or {}
    amount = float(data.get("amount", 0))
    uid_str = str(data.get("uid", "anonymous")).strip()
    if amount < 1:
        return jsonify({"error": "Minimum amount is $1"}), 400
    uid_int = (abs(hash(uid_str)) % (2**31 - 1)) or 1
    req = create_payment_request(uid_int, uid_str, amount)
    admin_secret_url = os.getenv("ADMIN_SECRET", "ADMIN_SECRET")
    _notify_admin(
        f"💰 <b>New Payment Request</b>\n"
        f"User: {uid_str}\n"
        f"Amount: ${amount:.2f}\n"
        f"Request ID: {req['id']}\n"
        f"Approve: https://{request.host}/api/admin/payment?id={req['id']}&action=approve&secret={admin_secret_url}\n"
        f"Deny: https://{request.host}/api/admin/payment?id={req['id']}&action=deny&secret={admin_secret_url}"
    )
    return jsonify({"id": req["id"], "amount": amount, "status": "pending"})


@app.route("/api/topup/notify", methods=["POST"])
def api_topup_notify():
    """User clicked 'I've Paid' — notify admin."""
    data = request.get_json(silent=True) or {}
    req_id = int(data.get("id", 0))
    uid_str = str(data.get("uid", "anonymous")).strip()
    req = get_payment_request(req_id)
    if not req:
        return jsonify({"error": "Payment request not found"}), 404
    _notify_admin(
        f"✅ <b>User Clicked 'I\\'ve Paid'</b>\n"
        f"User: {uid_str}\n"
        f"Amount: ${float(req['amount_usd']):.2f}\n"
        f"Request ID: {req_id}\n"
        f"Check your wallets and confirm."
    )
    return jsonify({"status": "notified"})


@app.route("/api/admin/payment")
def api_admin_payment():
    """Admin approves or denies a payment (via Telegram link)."""
    req_id = int(request.args.get("id", 0))
    action = request.args.get("action", "")
    secret = request.args.get("secret", "")
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if admin_secret and secret != admin_secret:
        return "Invalid secret", 403
    if action == "approve":
        approve_payment(req_id, "Admin approved")
        return "✅ Payment approved. User credited."
    elif action == "deny":
        deny_payment(req_id, "Admin denied")
        return "❌ Payment denied."
    return "Invalid action", 400


@app.route("/bulk/file/<token>")
def bulk_file(token):
    # Strip .txt/.csv extension if DeBounce appended it
    for ext in (".txt", ".csv"):
        if token.endswith(ext):
            token = token[:-len(ext)]
            break
    row = get_file(token)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return Response(
        row["content"],
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'},
    )


@app.route("/bulk/status")
def bulk_status():
    list_id = (request.args.get("list_id") or "").strip()
    status_data, err = bulk_api_status(list_id)
    if err:
        return jsonify({"error": err}), 400

    job = get_job(list_id)
    if job:
        status_data["submitted_total"] = job.get("submitted_total", 0)
        status_data["filtered_total"] = job.get("filtered_total", 0)
        status_data["invalid_total"] = job.get("invalid_total", 0)
        status_data["processed"] = job.get("processed", 0)

    if status_data.get("download_link") and job:
        complete_job(list_id, status_data["download_link"])
        process_queue()  # process next queued job

    return jsonify(status_data)


def _bulk_download_csv_text(download_link):
    res = requests.get(download_link, timeout=(API_CONNECT_TIMEOUT_S, API_READ_TIMEOUT_S))
    blob = res.content
    if res.headers.get("content-type", "").lower().startswith("application/zip") or blob[:4] == b"PK\x03\x04":
        zf = zipfile.ZipFile(io.BytesIO(blob))
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            members = zf.namelist()
        if not members:
            raise RuntimeError("Empty zip")
        blob = zf.read(members[0])
    return blob.decode("utf-8", errors="ignore")


@app.route("/bulk/results_stream")
def bulk_results_stream():
    list_id = (request.args.get("list_id") or "").strip()
    if not list_id:
        return jsonify({"error": "Missing list_id"}), 400

    job = get_job(list_id) or {}
    submitted_total = int(job.get("submitted_total") or 0)

    status_data, err = bulk_api_status(list_id)
    if err:
        return jsonify({"error": err}), 400
    if status_data.get("status") != "completed":
        return jsonify({"error": "List not completed yet"}), 409

    download_link = status_data.get("download_link") or job.get("download_link") or ""
    if not download_link:
        return jsonify({"error": "Missing download link"}), 400

    def generate():
        try:
            csv_text = _bulk_download_csv_text(download_link)
        except Exception:
            logger.exception("Failed to download bulk results")
            yield json.dumps({"type": "error", "reason": "Failed to download results"}) + "\n"
            yield json.dumps({"type": "complete"}) + "\n"
            return

        sample = csv_text[:4096]
        dialect = None
        try:
            dialect = csv.Sniffer().sniff(sample)
        except Exception:
            dialect = csv.excel

        reader = csv.DictReader(io.StringIO(csv_text, newline=None), dialect=dialect)
        i = 0
        total = submitted_total or 0

        for row in reader:
            i += 1
            total = max(total, i)

            row_lower = {}
            for k, v in (row or {}).items():
                if k is None:
                    continue
                row_lower[str(k).strip().lower()] = v

            email = normalize_email(row_lower.get("email") or row_lower.get("e-mail") or "")
            if not email:
                for v in row_lower.values():
                    if isinstance(v, str) and "@" in v:
                        extracted = extract_emails_from_text(v)
                        if extracted:
                            email = extracted[0]
                            break

            result_str = (row_lower.get("result") or "").strip()
            reason = (row_lower.get("reason") or "").strip()

            if not result_str:
                for k, v in row_lower.items():
                    if k.endswith("result") and isinstance(v, str) and v.strip():
                        result_str = v.strip()
                        break
            if not reason:
                for k, v in row_lower.items():
                    if k.endswith("reason") and isinstance(v, str) and v.strip():
                        reason = v.strip()
                        break

            status = "unknown"
            if result_str in ("Safe to Send", "Deliverable"):
                status = "valid"
            elif result_str == "Invalid":
                status = "invalid"
            elif result_str:
                status = "risky"

            yield json.dumps(
                {
                    "type": "progress",
                    "current": i,
                    "total": total,
                    "email": email or "",
                    "status": status,
                    "reason": reason or result_str or "Unknown",
                }
            ) + "\n"

        yield json.dumps({"type": "complete"}) + "\n"
        process_queue()  # process next queued job

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")

@app.route('/validate', methods=['POST'])
def validate():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    filter_free = request.form.get("filter_free") == "true"
    uid_str = request.form.get("uid", "anonymous").strip()
    uid_int = (abs(hash(uid_str)) % (2**31 - 1)) or 1

    # Rate limit: check if user already has a processing job
    active_user_jobs = count_active_jobs_for_user(uid_int)
    if active_user_jobs > 0:
        return jsonify({"error": "You already have a validation in progress. Wait for it to complete first."}), 429

    try:
        content = file.stream.read().decode("utf-8", errors="ignore")
    except Exception:
        return jsonify({"error": "Failed to read upload"}), 400

    extracted = extract_emails(content, file.filename)
    if not extracted:
        return jsonify({"error": "No emails found"}), 400

    seen = set()
    submit_emails = []
    invalid_total = 0
    filtered_total = 0

    for raw in extracted:
        email = normalize_email(raw)
        if not is_plausible_email(email):
            invalid_total += 1
            continue
        if email in seen:
            filtered_total += 1
            continue
        seen.add(email)
        if filter_free:
            domain = email.split("@", 1)[1].lower().strip()
            if domain in FREE_PROVIDERS:
                filtered_total += 1
                continue
        submit_emails.append(email)

    if not submit_emails:
        return jsonify({"error": "No valid emails to submit"}), 400
    if len(submit_emails) > BULK_MAX_EMAILS:
        return jsonify({"error": f"Too many emails (max {BULK_MAX_EMAILS})"}), 400

    payload_text = "\n".join(submit_emails) + "\n"
    payload_bytes = payload_text.encode("utf-8")
    if len(payload_bytes) > BULK_MAX_FILE_BYTES:
        return jsonify({"error": f"List file too large (max {BULK_MAX_FILE_BYTES} bytes)"}), 400

    token = bulk_store_file("bulk_list.txt", payload_bytes, user_id=uid_int)
    file_url = public_url(f"/bulk/file/{token}.txt")
    list_id, err = bulk_api_upload(file_url)
    if err:
        # If DeBounce is busy, queue the job instead of rejecting
        if "maximum number" in err.lower() or "existing request" in err.lower():
            fake_list_id = f"queued_{token[:16]}"
            queue_job(fake_list_id, len(submit_emails), filtered_total, invalid_total, token, user_id=uid_int)
            return jsonify({
                "mode": "queued",
                "list_id": fake_list_id,
                "message": "DeBounce is currently processing another list. Your job is queued and will be processed automatically.",
                "submitted_total": len(submit_emails),
                "filtered_total": filtered_total,
                "invalid_total": invalid_total,
            }), 202

        get_file(token)  # verify it exists, will be cleaned up by TTL
        return jsonify({"error": err}), 400

    create_job(list_id, len(submit_emails), filtered_total, invalid_total, token, user_id=uid_int)

    return jsonify(
        {
            "mode": "bulk",
            "list_id": list_id,
            "submitted_total": len(submit_emails),
            "filtered_total": filtered_total,
            "invalid_total": invalid_total,
        }
    )

# Initialize Neon schema on startup
init_schema()

if __name__ == '__main__':
    port = int(os.getenv("PORT", "5001"))
    host = os.getenv("HOST", "127.0.0.1")
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    print(f"Starting {APP_NAME} on http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
