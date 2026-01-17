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
    autoSortRunning = true;
    sortStats = { cardsProcessed: 0, errors: 0, startTime: new Date() };
    let consecutiveErrors = 0;
    const maxConsecutiveErrors = 5; // Stop after 5 consecutive errors
    
    // Configuration constants
    const WAIT_STABILIZATION = 2000;
    const WAIT_RETRY = 1000;
    const WAIT_SNAPSHOT = 1500;
    const WAIT_AFTER_MOTION = 2000;
    const MAX_ATTEMPTS = 3;
    const MIN_CONFIDENCE_SCORE = 70;
    
    document.getElementById('beginSortBtn').style.display = 'none';
    document.getElementById('stopSortBtn').style.display = 'block';
    updateSortStatus('Starting...');
    
    // Move to feeder position (A1) before starting
    try {
        const homeResponse = await fetch('/motion/goto/A1', { method: 'POST' });
        if (!homeResponse.ok) {
            throw new Error(`Failed to move to A1: ${homeResponse.status}`);
        }
        await homeResponse.json();
        await new Promise(resolve => setTimeout(resolve, WAIT_STABILIZATION));
    } catch (error) {
        console.error('Failed to move to feeder:', error);
        updateSortStatus(`Error: ${error.message}`);
        stopAutoSort();
        return;
    }
    
    while (autoSortRunning) {
        // Check for too many consecutive errors
        if (consecutiveErrors >= maxConsecutiveErrors) {
            updateSortStatus(`Stopped: ${maxConsecutiveErrors} consecutive errors`);
            stopAutoSort();
            break;
        }
        
        try {
            // Retry logic: attempt identification up to 3 times
            // IMPORTANT: All retries happen BEFORE picking up the card
            let identification = null;
            let assignment = null;
            let data = null;
            let attempts = 0;
            
            while (attempts < MAX_ATTEMPTS && !identification) {
                attempts++;
                if (attempts > 1) {
                    updateSortStatus(`Retry attempt ${attempts}/${MAX_ATTEMPTS}...`);
                    // Wait between retries, but DO NOT MOVE - retries are for the same card
                    await new Promise(resolve => setTimeout(resolve, WAIT_RETRY));
                }
                
                // Always wait before taking snapshot to ensure stability
                await new Promise(resolve => setTimeout(resolve, WAIT_SNAPSHOT));
                
                const response = await fetch('/camera/snapshot');
                if (!response.ok) {
                    throw new Error(`Snapshot failed: ${response.status} ${response.statusText}`);
                }
                data = await response.json();
                
                // Debug logging to diagnose identification issues
                console.log('Snapshot data received:', {
                    hasFrames: !!data?.frames,
                    frameCount: data?.frames?.length || 0,
                    hasOcrTextFrame: !!data.frames?.find(f => f.label === 'ocr_text'),
                    hasProcessing: !!data?.processing,
                    hasIdentification: !!data?.processing?.identification,
                    identificationBest: data?.processing?.identification?.best?.name || 'none',
                    identificationScore: data?.processing?.identification?.score || 0,
                    hasAssignment: !!data?.assignment,
                    assignmentCell: data?.assignment?.cell,
                });
                
                // Safety check: ensure data has required structure
                if (!data || !data.frames) {
                    console.error('Invalid snapshot response:', data);
                    updateSortStatus(`Invalid snapshot data (attempt ${attempts}/${MAX_ATTEMPTS})`);
                    identification = null;
                    continue;
                }
                
                // Get identification from data.processing (where it's actually stored)
                identification = data.processing?.identification;
                assignment = data.assignment;
                
                // Debug logging for identification results
                console.log('Identification check:', {
                    hasIdentification: !!identification,
                    hasBest: !!identification?.best,
                    cardName: identification?.best?.name,
                    score: identification?.score,
                    meetsThreshold: identification?.score >= MIN_CONFIDENCE_SCORE,
                    hasAssignment: !!assignment,
                    assignmentCell: assignment?.cell,
                    assignmentWarning: assignment?.warning,
                });
                
                // Check if there's a warning that should trigger a retry
                if (assignment && assignment.warning && !assignment.cell) {
                    console.warn(`Assignment warning: ${assignment.warning} - will retry`);
                    identification = null;
                    assignment = null;  // Reset assignment too so we don't use stale data
                    continue;
                }
                
                // Check if we got a valid identification with sufficient confidence
                if (identification && identification.best && identification.score >= MIN_CONFIDENCE_SCORE) {
                    console.log(`Match found: ${identification.best.name} with ${identification.score.toFixed(1)}% confidence`);
                    if (assignment && assignment.cell) {
                        console.log(`Assigned to cell: ${assignment.cell}`);
                    } else {
                        console.warn('Identification succeeded but assignment is missing or has no cell');
                    }
                    break;
                } else {
                    // Reset identification if confidence too low or missing
                    if (identification && identification.best && identification.score < MIN_CONFIDENCE_SCORE) {
                        console.log(`Low confidence: ${identification.score.toFixed(1)}% < ${MIN_CONFIDENCE_SCORE}% for ${identification.best?.name || 'unknown'} - retrying`);
                        identification = null;
                    } else if (!identification || !identification.best) {
                        console.log(`No identification found (attempt ${attempts}/${MAX_ATTEMPTS})`);
                        identification = null;
                    }
                }
            }
            
            // FIX #3: Validate data after retry loop
            if (!data || !data.frames) {
                throw new Error('No valid snapshot data after all retry attempts');
            }
            
            // Log final state after retry loop
            console.log('After retry loop:', {
                attempts,
                maxAttempts: MAX_ATTEMPTS,
                hasIdentification: !!identification,
                hasBest: !!identification?.best,
                cardName: identification?.best?.name,
                hasAssignment: !!assignment,
                hasCell: !!assignment?.cell,
                cellValue: assignment?.cell,
            });
            
            // Handle assignment (even without high-confidence identification)
            // Assignment can be present for:
            // 1. High confidence match (has identification.best)
            // 2. Low confidence match (diverted to K3)
            // 3. Unidentified cards (should go to ERR1/K3)
            if (assignment && assignment.cell) {
                const cardName = identification?.best?.name || 'Unknown card';
                const reason = assignment.reason || 'unspecified';
                
                sortStats.cardsProcessed++;
                consecutiveErrors = 0; // Reset consecutive error counter on success
                
                // Execute full pickup-and-delivery sequence (Z-axis pickup, move to cell, drop, return)
                try {
                    const pickupResponse = await fetch('/motion/home_z_and_extrude', { method: 'POST' });
                    if (!pickupResponse.ok) {
                        throw new Error(`Failed to execute pickup sequence: ${pickupResponse.status}`);
                    }
                    const pickupResult = await pickupResponse.json();
                    if (!pickupResult.ok) {
                        throw new Error(`Pickup sequence failed: ${pickupResult.error || 'unknown error'}`);
                    }
                    const movedTo = pickupResult.moved_to || assignment.cell;
                    
                    // Provide informative status message based on reason
                    if (reason.includes('low_confidence')) {
                        updateSortStatus(`Low confidence: ${cardName} → ${movedTo} (divert)`);
                    } else if (reason.includes('error') || reason.includes('unidentified')) {
                        updateSortStatus(`Unidentified → ${movedTo} (error)`);
                    } else {
                        updateSortStatus(`Sorted ${cardName} to ${movedTo}`);
                    }
                    
                    await new Promise(resolve => setTimeout(resolve, WAIT_AFTER_MOTION));
                } catch (error) {
                    console.error('Motion error:', error);
                    updateSortStatus(`Motion error: ${error.message}`);
                    sortStats.errors++;
                    consecutiveErrors++;
                    // System should already be back at start position after home_z_and_extrude
                    await new Promise(resolve => setTimeout(resolve, WAIT_AFTER_MOTION));
                }
            } else {
                // No assignment after retries - this is an error
                sortStats.errors++;
                consecutiveErrors++;
                updateSortStatus(`Failed to identify after ${MAX_ATTEMPTS} attempts`);
                
                // Move unidentified card to error pile (ERR1)
                try {
                    // Return to A1 first to ensure known position
                    const homeResponse = await fetch('/motion/goto/A1', { method: 'POST' });
                    if (!homeResponse.ok) {
                        throw new Error(`Failed to return to A1: ${homeResponse.status}`);
                    }
                    await homeResponse.json();
                    await new Promise(resolve => setTimeout(resolve, WAIT_RETRY));
                    
                    // Now move to error pile
                    const errorResponse = await fetch('/motion/goto/ERR1', { method: 'POST' });
                    if (!errorResponse.ok) {
                        // FIX #1: Check if ERR1 doesn't exist, use overflow cell K3 as fallback
                        if (errorResponse.status === 404 || errorResponse.status === 400) {
                            console.warn('ERR1 not available, using overflow cell K3');
                            const fallbackResponse = await fetch('/motion/goto/K3', { method: 'POST' });
                            if (!fallbackResponse.ok) {
                                throw new Error(`Failed to move to fallback cell K3: ${fallbackResponse.status}`);
                            }
                            await fallbackResponse.json();
                        } else {
                            throw new Error(`Failed to move to ERR1: ${errorResponse.status}`);
                        }
                    } else {
                        await errorResponse.json();
                    }
                    await new Promise(resolve => setTimeout(resolve, WAIT_RETRY));
                    
                    // Return to feeder position after error
                    const returnResponse = await fetch('/motion/goto/A1', { method: 'POST' });
                    if (!returnResponse.ok) {
                        throw new Error(`Failed to return to A1: ${returnResponse.status}`);
                    }
                    await returnResponse.json();
                    await new Promise(resolve => setTimeout(resolve, WAIT_AFTER_MOTION));
                } catch (error) {
                    console.error('Error handling failed identification:', error);
                    updateSortStatus(`Error: ${error.message}`);
                    consecutiveErrors++;
                    // Last ditch effort to return to A1
                    try {
                        const lastChanceResponse = await fetch('/motion/goto/A1', { method: 'POST' });
                        if (lastChanceResponse.ok) {
                            await lastChanceResponse.json();
                            await new Promise(resolve => setTimeout(resolve, WAIT_AFTER_MOTION));
                        }
                    } catch (finalError) {
                        console.error('Final recovery failed:', finalError);
                        updateSortStatus(`Critical error: System may be in unknown position`);
                    }
                }
            }
        } catch (error) {
            sortStats.errors++;
            consecutiveErrors++;
            updateSortStatus(`Error: ${error.message}`);
            // Try to recover to A1 on any unexpected error
            try {
                const emergencyResponse = await fetch('/motion/goto/A1', { method: 'POST' });
                if (emergencyResponse.ok) {
                    await emergencyResponse.json();
                    await new Promise(resolve => setTimeout(resolve, WAIT_AFTER_MOTION));
                    updateSortStatus(`Recovered to feeder after error`);
                }
            } catch (emergencyError) {
                console.error('Emergency recovery failed:', emergencyError);
            }
            await new Promise(resolve => setTimeout(resolve, WAIT_AFTER_MOTION));
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
