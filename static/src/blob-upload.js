import { upload } from '@vercel/blob/client';

const showStatus = (form, text, isError = false) => {
  let status = form.querySelector('[data-blob-upload-status]');
  if (!status) {
    status = document.createElement('p');
    status.dataset.blobUploadStatus = '';
    status.setAttribute('role', 'status');
    form.append(status);
  }
  status.textContent = text;
  status.classList.toggle('ajax-error', isError);
};

document.addEventListener('submit', async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.blobPurpose) return;
  const fileInput = form.querySelector('input[type="file"]');
  const file = fileInput?.files?.[0];
  if (!file) return;

  event.preventDefault();
  event.stopImmediatePropagation();
  const submitButton = event.submitter || form.querySelector('button[type="submit"]');
  if (submitButton) submitButton.disabled = true;
  showStatus(form, 'Uploading securely…');

  try {
    const csrfToken = form.querySelector('[name="csrfmiddlewaretoken"]')?.value;
    const tokenResponse = await fetch(form.dataset.blobTokenUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
      },
      body: JSON.stringify({
        purpose: form.dataset.blobPurpose,
        name: file.name,
        size: file.size,
      }),
    });
    const ticket = await tokenResponse.json();
    if (!tokenResponse.ok) throw new Error(ticket.error || 'Upload authorization failed.');

    const blob = await upload(ticket.pathname, file, {
      access: 'private',
      handleUploadUrl: form.dataset.blobUploadUrl,
      clientPayload: ticket.token,
      contentType: ticket.content_type,
      multipart: file.size > 4 * 1024 * 1024,
      onUploadProgress: ({ percentage }) => {
        showStatus(form, `Uploading securely… ${Math.floor(percentage)}%`);
      },
    });
    if (blob.pathname !== ticket.pathname) {
      throw new Error('Uploaded file path did not match its authorization.');
    }

    form.querySelector('[name="blob_upload_token"]').value = ticket.token;
    form.querySelector('[name="blob_upload_path"]').value = blob.pathname;
    fileInput.value = '';
    form.dataset.blobPrepared = 'true';
    showStatus(form, 'Upload complete. Saving…');
    if (submitButton) form.requestSubmit(submitButton);
    else form.requestSubmit();
  } catch (error) {
    showStatus(form, error.message || 'The upload failed. Please try again.', true);
    if (submitButton) submitButton.disabled = false;
  }
}, true);
