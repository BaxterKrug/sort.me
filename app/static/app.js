// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    initializeCellGrid();
    updateStatus();
    setInterval(updateStatus, 2000);
    refreshAutoSortStatus();
    setInterval(refreshAutoSortStatus, 2000);
    refreshIdSourceStatus();
    
    // Add handler for sort type dropdown
    const sortTypeSelect = document.getElementById('sortType');
    if (sortTypeSelect) {
        sortTypeSelect.addEventListener('change', async function() {
            const sortMode = this.value;
            try {
                const response = await fetch('/sorting/mode', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode: sortMode })
                });
                const data = await response.json();
                if (data.ok) {
                    console.log(`Sort mode changed to: ${data.label} (${data.active})`);
                } else {
                    console.error('Failed to change sort mode:', data.message);
                }
            } catch (error) {
                console.error('Error changing sort mode:', error);
            }
            initializeCellGrid();
        });
    }
});

function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
    }
    return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

async function refreshIdSourceStatus() {
    const statusDiv = document.getElementById('idSourceStatus');
    if (!statusDiv) return;
    try {
        const response = await fetch('/id/source/status');
        const data = await response.json();
        const source = data?.source || {};
        if (!source.exists) {
            statusDiv.innerHTML = '<div style="color:#ffa500;">No imported card source found. Import your Scryfall default cards JSON.</div>';
            return;
        }
        statusDiv.innerHTML = `
            <div style="color:#00ff9f; font-weight:600; margin-bottom:0.35rem;">Card source ready</div>
            <div style="font-size:0.85rem; color:#a0a8be;">Path: ${source.path || 'unknown'}</div>
            <div style="font-size:0.85rem; color:#a0a8be;">Size: ${formatBytes(source.size_bytes || 0)}</div>
            <div style="font-size:0.85rem; color:#a0a8be;">Updated: ${source.modified_at || 'unknown'}</div>
        `;
    } catch (error) {
        statusDiv.innerHTML = `<div style="color:#e94560;">Failed to read source status: ${error.message}</div>`;
    }
}

async function importScryfallSource() {
    const fileInput = document.getElementById('scryfallFileInput');
    const btn = document.getElementById('importScryfallBtn');
    const statusDiv = document.getElementById('idSourceStatus');

    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        if (statusDiv) {
            statusDiv.innerHTML = '<div style="color:#ffa500;">Choose a Scryfall JSON file first.</div>';
        }
        return;
    }

    const selectedFile = fileInput.files[0];
    const formData = new FormData();
    formData.append('file', selectedFile, selectedFile.name);

    if (btn) btn.disabled = true;
    if (statusDiv) {
        statusDiv.innerHTML = '<div style="color:#00d9ff;">Importing card source file...</div>';
    }

    try {
        const response = await fetch('/id/source/import', {
            method: 'POST',
            body: formData,
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
            throw new Error(data.detail || data.message || `Import failed (${response.status})`);
        }
        if (statusDiv) {
            const info = data.import || {};
            statusDiv.innerHTML = `
                <div style="color:#00ff9f; font-weight:600; margin-bottom:0.35rem;">Import complete</div>
                <div style="font-size:0.85rem; color:#a0a8be;">File: ${info.filename || selectedFile.name}</div>
                <div style="font-size:0.85rem; color:#a0a8be;">Copied: ${formatBytes(info.bytes || selectedFile.size || 0)}</div>
                <div style="font-size:0.85rem; color:#a0a8be;">Validated: ${info.validated ? 'yes' : 'basic'}</div>
            `;
        }
        fileInput.value = '';
        await refreshIdSourceStatus();
    } catch (error) {
        if (statusDiv) {
            statusDiv.innerHTML = `<div style="color:#e94560;">Import failed: ${error.message}</div>`;
        }
    } finally {
        if (btn) btn.disabled = false;
    }
}

let autoSortRunning = false;
let sortStats = {
    cardsProcessed: 0,
    errors: 0,
    startTime: null,
    lastElapsedSeconds: 0
};

async function updateStatus() {
    try {
        const response = await fetch('/status');
        const data = await response.json();
        document.getElementById('motionStatus').textContent = `Motion: ${data.motion?.status || 'Unknown'}`;
        document.getElementById('cameraStatus').textContent = `Camera: ${data.camera?.status || 'Unknown'}`;
    } catch (error) {
        console.error('Status update failed:', error);
    }
}

async function refreshAutoSortStatus() {
    try {
        const response = await fetch('/auto_sort/status');
        if (!response.ok) return;
        const data = await response.json();
        autoSortRunning = !!data.running;

        const stats = data.stats || {};
        sortStats.cardsProcessed = stats.cards_processed || 0;
        sortStats.errors = stats.errors || 0;
        sortStats.startTime = stats.started_at ? new Date(stats.started_at) : sortStats.startTime;

        const runtime = data.runtime || {};
        if (autoSortRunning) {
            document.getElementById('beginSortBtn').style.display = 'none';
            document.getElementById('stopSortBtn').style.display = 'block';
            updateSortStatus(runtime.message || 'Auto-sort running');
        } else {
            document.getElementById('beginSortBtn').style.display = 'block';
            document.getElementById('stopSortBtn').style.display = 'none';
            sortStats.startTime = null;
            if (runtime.message) {
                updateSortStatus(runtime.message);
            }
        }
    } catch (error) {
        console.error('Auto-sort status update failed:', error);
    }
}

function initializeCellGrid() {
    const grid = document.getElementById('cellGrid');
    grid.innerHTML = '';
    const letters = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K'];
    const rows = 3; // Only 3 rows
    const sortType = document.getElementById('sortType');
    const isPrice = sortType && sortType.value === 'price';
    // Each row contains cells from all letters for that row number
    for (let row = 1; row <= rows; row++) {
        for (let letter of letters) {
            const cellId = letter + row; // A1, B1, C1, ..., K1, then A2, B2, C2, ..., K2, etc.
            const button = document.createElement('button');
            button.className = 'cell-button';
            button.textContent = cellId;
            button.onclick = () => goToCell(cellId);
            if (isPrice) {
                // Price sort: top row highlighted
                if (row === 1) {
                    button.classList.add('feeder');
                }
            } else {
                // Alphabetical sort: left column highlighted
                if (letter === 'A') {
                    button.classList.add('feeder');
                }
            }
            if (!isPrice && cellId === 'K3') {
                button.classList.add('overflow');
            }
            grid.appendChild(button);
        }
    }
}

async function goToCell(cellId) {
    try {
        const response = await fetch(`/motion/goto/${cellId}`, { method: 'POST' });
        const data = await response.json();
        console.log(`Moving to cell ${cellId}:`, data);
    } catch (error) {
        console.error(`Failed to move to cell ${cellId}:`, error);
        alert(`Failed to move to cell ${cellId}`);
    }
}

async function jogMotion(axis, distance) {
    try {
        // Get the jog distance from dropdown if 'jogDistance' is passed
        const distanceSelect = document.getElementById('jogDistance');
        const jogDistance = parseFloat(distanceSelect ? distanceSelect.value : 10);
        
        // Determine actual distance value
        let actualDistance = 0;
        if (distance === 'jogDistance') {
            actualDistance = jogDistance;
        } else if (distance === '-jogDistance') {
            actualDistance = -jogDistance;
        } else {
            actualDistance = parseFloat(distance) || 0;
        }
        
        // Determine axis (X, Y, or Z)
        let actualAxis = '';
        if (axis === 'jogDistance') {
            actualAxis = 'X';
            actualDistance = jogDistance;
        } else if (axis === '-jogDistance') {
            actualAxis = 'X';
            actualDistance = -jogDistance;
        } else if (typeof axis === 'string' && axis.match(/^[XYZ]$/i)) {
            actualAxis = axis.toUpperCase();
        } else if (typeof axis === 'number') {
            // Old API - x,y,z parameters - convert to new API
            // This branch shouldn't be hit with the new button setup
            console.log('Old API call detected, please update buttons');
            return;
        }
        
        if (!actualAxis || actualDistance === 0) {
            console.log('Skipping jog: no axis or zero distance');
            return;
        }
        
        console.log(`Jogging ${actualAxis} by ${actualDistance}mm`);
        
        const response = await fetch('/motion/jog', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ axis: actualAxis, distance: actualDistance })
        });
        const data = await response.json();
        console.log('Jog result:', data);
    } catch (error) {
        console.error('Jog failed:', error);
        alert('Motion jog failed');
    }
}

async function homeMotion() {
    try {
        const response = await fetch('/motion/home', { method: 'POST' });
        const data = await response.json();
        console.log('Home result:', data);
    } catch (error) {
        console.error('Home failed:', error);
        alert('Homing failed');
    }
}

async function calibrateSorter() {
    const btn = document.getElementById('calibrateSorterBtn');
    const statusDiv = document.getElementById('calibrateStatus');
    btn.disabled = true;
    statusDiv.innerHTML = '<p style="text-align:center; color:#00d9ff;">Calibrating...</p>';
    try {
        const response = await fetch('/motion/calibrate_sorter', { method: 'POST' });
        const data = await response.json();
        if (data.ok) {
            statusDiv.innerHTML = '<p style="color:#00ff88;">Calibration complete &mdash; homed, Z at focal length, at A1</p>';
        } else {
            statusDiv.innerHTML = '<p style="color:#ff4444;">Calibration failed: ' + (data.detail || 'Unknown error') + '</p>';
        }
        console.log('Calibrate sorter result:', data);
    } catch (error) {
        console.error('Calibrate sorter failed:', error);
        statusDiv.innerHTML = '<p style="color:#ff4444;">Calibration failed: ' + error.message + '</p>';
    } finally {
        btn.disabled = false;
    }
}

document.getElementById('estopBtn').addEventListener('click', async function() {
    if (confirm('Are you sure you want to trigger EMERGENCY STOP?')) {
        try {
            await fetch('/motion/estop', { method: 'POST' });
            alert('E-STOP triggered!');
            if (autoSortRunning) {
                stopAutoSort();
            }
        } catch (error) {
            console.error('E-STOP failed:', error);
        }
    }
});

async function takeSingleSnapshot() {
    const btn = document.getElementById('snapshotBtn');
    const resultDiv = document.getElementById('snapshotResult');
    btn.disabled = true;
    resultDiv.innerHTML = '<p style="text-align:center; color:#00d9ff;">Capturing...</p>';
    try {
        const response = await fetch('/camera/snapshot');
        const data = await response.json();
        
        // Check for QR code detection first
        const qrCode = data.qr_code;
        if (qrCode && qrCode.detected) {
            // Extract column letter from cell (e.g., "A1" -> "A")
            const cellLetter = qrCode.cell ? qrCode.cell.charAt(0) : '';
            const commandLabel = qrCode.command === 'endstep' ? 'End of Column' : qrCode.command;
            
            // Show QR detection result
            let qrHtml = `
                <div class="card-result" style="border-color: #00ff9f;">
                    <h3 style="color: #00ff9f;">📱 QR Code Detected</h3>
                    <div class="card-info">
                        <div class="card-info-row">
                            <span class="card-info-label">QR Data:</span>
                            <span class="card-info-value">${qrCode.data || 'Unknown'}</span>
                        </div>
                        ${qrCode.cell ? `
                        <div class="card-info-row">
                            <span class="card-info-label">Column:</span>
                            <span class="card-info-value" style="font-size: 1.2em; font-weight: bold;">${cellLetter}</span>
                        </div>
                        ` : ''}
                        <div class="card-info-row">
                            <span class="card-info-label">Command:</span>
                            <span class="card-info-value">${commandLabel}</span>
                        </div>
                        <div class="card-info-row">
                            <span class="card-info-label">Executed:</span>
                            <span class="card-info-value" style="color: ${qrCode.executed ? '#00ff9f' : '#ffa500'};">
                                ${qrCode.executed ? 'Yes' : 'No'}
                            </span>
                        </div>
                    </div>
                    ${qrCode.message ? `<div class="card-destination" style="background: #1a3a2a; border-color: #00ff9f;">📦 ${qrCode.message}</div>` : ''}
                </div>
            `;
            
            resultDiv.innerHTML = qrHtml;
            
            // Show popup alert for column completion
            if (qrCode.command === 'endstep' && qrCode.cell) {
                setTimeout(() => {
                    alert(`Column ${cellLetter} is DONE!\n\nAdvanced from ${qrCode.cell} to next column.`);
                }, 100);
            }
            return;
        }
        
        // Try multiple paths to find identification (zone_ocr has both orientation results)
        const zoneOcr = data.processing?.zone_ocr;
        const identification = zoneOcr?.identification || data.processing?.identification;
        const assignment = data.assignment;
        
        // Check if identification was rejected
        if (assignment?.warning) {
            resultDiv.innerHTML = `
                <div class="card-result" style="border-color: #ffa500;">
                    <h3 style="color: #ffa500;">⚠ ${assignment.warning}</h3>
                    <p style="margin-top: 10px; color: #ccc;">OCR Text:</p>
                    <div style="font-family: monospace; font-size: 12px; color: #aaa; margin-top: 5px;">
                        Normal: ${zoneOcr?.normal?.name || 'N/A'}<br>
                        180°: ${zoneOcr?.rotated_180?.name || 'N/A'}
                    </div>
                </div>
            `;
            return;
        }
        
        if (identification && identification.best) {
            const card = identification.best;
            const score = identification.score || 0;
            const scoreColor = score >= 80 ? '#00ff9f' : score >= 60 ? '#ffa500' : '#e94560';
            
            resultDiv.innerHTML = `
                <div class="card-result">
                    <h3>✓ Card Identified</h3>
                    <div class="card-info">
                        <div class="card-info-row">
                            <span class="card-info-label">Name:</span>
                            <span class="card-info-value">${card.name || 'Unknown'}</span>
                        </div>
                        <div class="card-info-row">
                            <span class="card-info-label">Set:</span>
                            <span class="card-info-value">${card.set_name || card.set || 'Unknown'}</span>
                        </div>
                        <div class="card-info-row">
                            <span class="card-info-label">Collector:</span>
                            <span class="card-info-value">${card.collector_number || 'N/A'}</span>
                        </div>
                        <div class="card-info-row">
                            <span class="card-info-label">Confidence:</span>
                            <span class="card-info-value" style="color: ${scoreColor}; font-weight: bold;">${score.toFixed(1)}%</span>
                        </div>
                        ${zoneOcr?.selected_orientation ? `
                        <div class="card-info-row">
                            <span class="card-info-label">Orientation:</span>
                            <span class="card-info-value">${zoneOcr.selected_orientation === 'normal' ? 'Correct' : '180° rotated'}</span>
                        </div>
                        ` : ''}
                    </div>
                    ${assignment ? `<div class="card-destination">📦 Assigned to Cell: ${assignment.cell} (${assignment.reason})</div>` : ''}
                </div>
            `;
        } else {
            resultDiv.innerHTML = `
                <div class="card-result" style="border-color: #e94560;">
                    <h3 style="color: #e94560;">✗ No Match Found</h3>
                    <p style="margin-top: 10px; color: #ccc;">OCR Text:</p>
                    <div style="font-family: monospace; font-size: 12px; color: #aaa; margin-top: 5px;">
                        Normal: ${zoneOcr?.normal?.name || 'N/A'}<br>
                        180°: ${zoneOcr?.rotated_180?.name || 'N/A'}
                    </div>
                </div>
            `;
        }
    } catch (error) {
        resultDiv.innerHTML = `<div style="color: #e94560;">❌ Error: ${error.message}</div>`;
    } finally {
        btn.disabled = false;
    }
}

/**
 * Auto-sort main loop
 * 
 * Improvements implemented:
 * - FIX #1: ERR1 error handling with K3 fallback if ERR1 unavailable
 * - FIX #2: Configuration constants extracted for maintainability
 * - FIX #3: Data validation after retry loop to prevent null reference errors
 * - FIX #4: Separate identification vs assignment errors (assignment failures don't increment consecutiveErrors)
 */
async function beginAutoSort() {
    if (autoSortRunning) return;

    const sortModeSelect = document.getElementById('sortType');
    const payload = {
        sort_mode: sortModeSelect ? sortModeSelect.value : undefined,
    };

    try {
        const response = await fetch('/auto_sort/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
            throw new Error(data.message || `Auto-sort start failed: ${response.status}`);
        }

        autoSortRunning = true;
        sortStats = {
            cardsProcessed: data.stats?.cards_processed || 0,
            errors: data.stats?.errors || 0,
            startTime: data.stats?.started_at ? new Date(data.stats.started_at) : new Date(),
            lastElapsedSeconds: 0,
        };
        document.getElementById('beginSortBtn').style.display = 'none';
        document.getElementById('stopSortBtn').style.display = 'block';
        updateSortStatus(data.runtime?.message || 'Auto-sort started');
    } catch (error) {
        console.error('Failed to start auto-sort:', error);
        autoSortRunning = false;
        updateSortStatus(`Error: ${error.message}`);
        document.getElementById('beginSortBtn').style.display = 'block';
        document.getElementById('stopSortBtn').style.display = 'none';
    }
}

function stopAutoSort() {
    fetch('/auto_sort/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            autoSortRunning = false;
            sortStats.cardsProcessed = data.stats?.cards_processed || sortStats.cardsProcessed;
            sortStats.errors = data.stats?.errors || sortStats.errors;
            sortStats.lastElapsedSeconds = sortStats.startTime ? Math.max(0, Math.floor((new Date() - sortStats.startTime) / 1000)) : sortStats.lastElapsedSeconds;
            sortStats.startTime = null;
            updateSortStatus(data.runtime?.message || 'Stopped');
            document.getElementById('beginSortBtn').style.display = 'block';
            document.getElementById('stopSortBtn').style.display = 'none';
        })
        .catch(error => {
            console.error('Failed to stop auto-sort:', error);
            autoSortRunning = false;
            sortStats.startTime = null;
            document.getElementById('beginSortBtn').style.display = 'block';
            document.getElementById('stopSortBtn').style.display = 'none';
            updateSortStatus(`Error stopping auto-sort: ${error.message}`);
        });
}

function updateSortStatus(message) {
    const elapsed = sortStats.startTime ? Math.floor((new Date() - sortStats.startTime) / 1000) : (sortStats.lastElapsedSeconds || 0);
    if (autoSortRunning) {
        sortStats.lastElapsedSeconds = elapsed;
    }
    document.getElementById('sortStatus').innerHTML = `
        <div style="color: #00d9ff; margin-bottom: 1rem;">${message}</div>
        <div class="sort-stats">
            <div class="stat-box"><div class="stat-value">${sortStats.cardsProcessed}</div><div class="stat-label">Sorted</div></div>
            <div class="stat-box"><div class="stat-value">${sortStats.errors}</div><div class="stat-label">Errors</div></div>
            <div class="stat-box"><div class="stat-value">${elapsed}s</div><div class="stat-label">Time</div></div>
        </div>
    `;
}
