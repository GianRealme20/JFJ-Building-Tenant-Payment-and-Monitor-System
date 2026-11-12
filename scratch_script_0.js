
    // Helper to format date strings into YYYY-MM-DD for HTML5 date inputs
    function normalizeDateForInput(dateStr) {
        if (!dateStr) return '';
        dateStr = String(dateStr).trim();
        if (dateStr.includes('-')) {
            const parts = dateStr.split('-');
            if (parts.length === 3 && parts[0].length === 4) return dateStr;
        }
        if (dateStr.includes('/')) {
            const parts = dateStr.split('/');
            if (parts.length === 3) {
                const y = parts[2].length === 4 ? parts[2] : '20' + parts[2];
                const m = parts[0].padStart(2, '0');
                const d = parts[1].padStart(2, '0');
                return `${y}-${m}-${d}`;
            }
        }
        return dateStr;
    }

    // Set default date to today in the add payment form
    document.addEventListener('DOMContentLoaded', () => {
        const dateInput = document.getElementById('modal_date');
        if (dateInput && !dateInput.value) {
            const today = new Date().toISOString().split('T')[0];
            dateInput.value = today;
        }

        // Global Event Delegation for Edit and Delete buttons (no inline quotes issues)
        document.addEventListener('click', (e) => {
            const editBtn = e.target.closest('.btn-edit-payment');
            if (editBtn) {
                const d = editBtn.dataset;
                openEditPaymentModal(d.id, d.room, d.tenant, d.due, d.paid, d.date, d.method, d.category, d.notes);
                return;
            }

            const delBtn = e.target.closest('.btn-delete-payment');
            if (delBtn) {
                const d = delBtn.dataset;
                confirmDeleteRecord(d.id, d.tenant, d.room);
                return;
            }
        });
    });

    // Room Filter selector redirect
    function handleRoomFilter(roomValue) {
        if (roomValue) {
            window.location.href = "{{ url_for('dashboard') }}?room=" + encodeURIComponent(roomValue);
        } else {
            window.location.href = "{{ url_for('dashboard') }}";
        }
    }

    // Modal Control: Add Payment
    function openAddPaymentModal() {
        const modal = document.getElementById('addPaymentModal');
        modal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
        calculateBalancePreview('add');
        setTimeout(() => document.getElementById('modal_room_no').focus(), 50);
    }

    function closeAddPaymentModal() {
        const modal = document.getElementById('addPaymentModal');
        modal.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
    }

    // Modal Control: Edit Payment
    function openEditPaymentModal(id, room, tenant, due, paid, date, method, category, notes) {
        const modal = document.getElementById('editPaymentModal');
        const form = document.getElementById('editPaymentForm');

        form.action = "{{ url_for('edit_payment', payment_id='__ID__') }}".replace('__ID__', id);

        document.getElementById('edit_room_no').value = room || '';
        document.getElementById('edit_tenant_name').value = tenant || '';
        document.getElementById('edit_payment_due').value = due || '';
        document.getElementById('edit_amount_paid').value = paid || '';
        document.getElementById('edit_date').value = normalizeDateForInput(date);
        document.getElementById('edit_payment_method').value = method || 'Cash';
        document.getElementById('edit_category').value = category || 'Rent';
        document.getElementById('edit_notes').value = notes || '';

        modal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
        calculateBalancePreview('edit');
    }

    function closeEditPaymentModal() {
        const modal = document.getElementById('editPaymentModal');
        modal.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
    }

    // Live Balance calculation preview in modals
    function calculateBalancePreview(mode) {
        const prefix = mode === 'edit' ? 'edit_' : 'modal_';
        const target = mode === 'edit' ? 'edit' : 'add';

        const due = parseFloat(document.getElementById(prefix + 'payment_due').value) || 0;
        const paid = parseFloat(document.getElementById(prefix + 'amount_paid').value) || 0;
        const balance = Math.max(0, due - paid);

        const balanceEl = document.getElementById('previewBalance_' + target);
        const statusEl = document.getElementById('previewStatus_' + target);

        balanceEl.textContent = '₱' + balance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

        if (due > 0 && paid >= due) {
            statusEl.textContent = 'Paid';
            statusEl.className = 'inline-flex items-center px-2 py-0.5 rounded text-xs font-bold bg-emerald-100 text-emerald-800';
            balanceEl.className = 'text-base font-bold text-emerald-600';
        } else {
            statusEl.textContent = 'Pending';
            statusEl.className = 'inline-flex items-center px-2 py-0.5 rounded text-xs font-bold bg-amber-100 text-amber-800';
            balanceEl.className = 'text-base font-bold text-rose-600';
        }
    }

    // Modal Control: Delete Confirmation
    function confirmDeleteRecord(id, tenantName, roomNo) {
        const modal = document.getElementById('deleteModal');
        const form = document.getElementById('deleteForm');
        document.getElementById('deleteTenantName').textContent = tenantName;
        document.getElementById('deleteRoomNo').textContent = "Room " + roomNo;

        form.action = "{{ url_for('delete_payment', payment_id='__ID__') }}".replace('__ID__', id);

        modal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
    }

    function closeDeleteModal() {
        const modal = document.getElementById('deleteModal');
        modal.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
    }

    // Close modals on Escape key
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeAddPaymentModal();
            closeEditPaymentModal();
            closeDeleteModal();
        }
    });

    // Client-side instant live table search filter
    function filterPaymentsTable() {
        const query = document.getElementById('tableSearchInput').value.toLowerCase().trim();
        const rows = document.querySelectorAll('.payment-row');
        let visibleCount = 0;

        rows.forEach(row => {
            const rowText = row.textContent.toLowerCase();
            if (rowText.includes(query)) {
                row.style.display = '';
                visibleCount++;
            } else {
                row.style.display = 'none';
            }
        });

        const noMatchMsg = document.getElementById('noMatchMessage');
        const countBadge = document.getElementById('recordCountBadge');

        if (noMatchMsg) {
            if (visibleCount === 0 && rows.length > 0) {
                noMatchMsg.classList.remove('hidden');
            } else {
                noMatchMsg.classList.add('hidden');
            }
        }

        if (countBadge) {
            countBadge.textContent = visibleCount + ' entries';
        }
    }
