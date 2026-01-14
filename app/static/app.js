// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    initializeCellGrid();
    updateStatus();
    setInterval(updateStatus, 2000);
    
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
        });
    }
});

let autoSortRunning = false;
let sortStats = {
    cardsProcessed: 0,
    errors: 0,
    startTime: null
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

function initializeCellGrid() {
    const grid = document.getElementById('cellGrid');
    grid.innerHTML = '';
    const letters = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K'];
    const rows = 3; // Only 3 rows
    // Each row contains cells from all letters for that row number
    for (let row = 1; row <= rows; row++) {
        for (let letter of letters) {
            const cellId = letter + row; // A1, B1, C1, ..., K1, then A2, B2, C2, ..., K2, etc.
            const button = document.createElement('button');
            button.className = 'cell-button';
            button.textContent = cellId;
            button.onclick = () => goToCell(cellId);
            if (['A1', 'A2', 'A3'].includes(cellId)) {
                button.classList.add('feeder');
            }
            if (cellId === 'K3') {
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
        const ocrTextFrame = data.frames?.find(f => f.label === 'ocr_text');
        const identification = ocrTextFrame?.meta?.identification;
        const assignment = data.assignment;
        if (identification && identification.best) {
            const card = identification.best;
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
                            <span class="card-info-label">Price:</span>
                            <span class="card-info-value">$${card.prices?.usd || 'N/A'}</span>
                        </div>
                        <div class="card-info-row">
                            <span class="card-info-label">Score:</span>
                            <span class="card-info-value">${identification.score.toFixed(1)}%</span>
                        </div>
                    </div>
                    ${assignment ? `<div class="card-destination">📦 Cell ${assignment.cell}</div>` : ''}
                </div>
            `;
        } else {
            resultDiv.innerHTML = '<div class="card-result" style="border-color: #e94560;"><h3 style="color: #e94560;">✗ No Match</h3></div>';
        }
    } catch (error) {
        resultDiv.innerHTML = `<div style="color: #e94560;">❌ Error: ${error.message}</div>`;
    } finally {
        btn.disabled = false;
    }
}

async function beginAutoSort() {
    if (autoSortRunning) return;
    autoSortRunning = true;
    sortStats = { cardsProcessed: 0, errors: 0, startTime: new Date() };
    document.getElementById('beginSortBtn').style.display = 'none';
    document.getElementById('stopSortBtn').style.display = 'block';
    updateSortStatus('Starting...');
    while (autoSortRunning) {
        try {
            const response = await fetch('/camera/snapshot');
            const data = await response.json();
            const ocrTextFrame = data.frames?.find(f => f.label === 'ocr_text');
            const identification = ocrTextFrame?.meta?.identification;
            const assignment = data.assignment;
            if (identification && identification.best && assignment) {
                sortStats.cardsProcessed++;
                await fetch(`/motion/goto/${assignment.cell}`, { method: 'POST' });
                updateSortStatus(`Sorted ${identification.best.name} to ${assignment.cell}`);
                await new Promise(resolve => setTimeout(resolve, 1000));
            } else {
                sortStats.errors++;
                await fetch('/motion/goto/ERR1', { method: 'POST' });
                updateSortStatus('Error - moved to overflow');
            }
        } catch (error) {
            sortStats.errors++;
            updateSortStatus(`Error: ${error.message}`);
            await new Promise(resolve => setTimeout(resolve, 2000));
        }
    }
}

function stopAutoSort() {
    autoSortRunning = false;
    document.getElementById('beginSortBtn').style.display = 'block';
    document.getElementById('stopSortBtn').style.display = 'none';
    updateSortStatus('Stopped');
}

function updateSortStatus(message) {
    const elapsed = sortStats.startTime ? Math.floor((new Date() - sortStats.startTime) / 1000) : 0;
    document.getElementById('sortStatus').innerHTML = `
        <div style="color: #00d9ff; margin-bottom: 1rem;">${message}</div>
        <div class="sort-stats">
            <div class="stat-box"><div class="stat-value">${sortStats.cardsProcessed}</div><div class="stat-label">Sorted</div></div>
            <div class="stat-box"><div class="stat-value">${sortStats.errors}</div><div class="stat-label">Errors</div></div>
            <div class="stat-box"><div class="stat-value">${elapsed}s</div><div class="stat-label">Time</div></div>
        </div>
    `;
}
