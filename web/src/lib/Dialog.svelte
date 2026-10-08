<script>
  import { Modal, ModalBody } from '@immich/ui';

  /** @type {{ title: string, size?: 'tiny'|'small'|'medium'|'large'|'giant', onClose: () => void, children: import('svelte').Snippet }} */
  let { title, size = 'small', onClose, children } = $props();
</script>

<!-- Like BasicModal, but without the footer "Close" button: the X in the header (and Esc / backdrop click) closes it. -->
<Modal {title} {size} {onClose} icon={false} closeOnBackdropClick>
  <ModalBody>{@render children()}</ModalBody>
</Modal>
