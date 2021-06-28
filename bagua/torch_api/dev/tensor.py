#!/usr/bin/env python3
from bagua.torch_api.communication import _get_global_state
import torch
import bagua_core as B
from bagua.torch_api.utils import to_bagua_datatype
import gorilla

@gorilla.patches(torch.Tensor)
class BaguaTensor(object):
    """
    This class patch torch.Tensor with additional methods.
    """

    def to_bagua_tensor(self, name: str):
        """
        Convert a PyTorch tensor or parameter to Bagua tensor and return it.
        A Bagua tensor is required to use Bagua's communication algorithms.

        Args:
            name (str): the unique name of the tensor

        Returns:
            The original tensor with Bagua tensor attributes initialized.
        """
        self.bagua_tensor_name = name
        self.backend_tensor = B.BaguaTensorPy(
            ptr=self.data_ptr(),
            num_elem=self.numel(),
            num_elem_allocated=self.__dict__.get("allocated_size", self.numel()),
            dtype=to_bagua_datatype(self.dtype),
            device_id=self.device.index,
        )
        self._bagua_backend = _get_global_state().get_backend()
        self._bagua_ready_event = torch.cuda.Event()
        self._bagua_bucket = None
        return self

    def bagua_ensure_grad(self) -> torch.Tensor:
        """
        Return the gradient of current parameter. Create a zero gradient tensor
        if not exist.
        """
        if hasattr(self, "grad") and self.grad is not None:
            return self.grad
        elif isinstance(self, torch.nn.Parameter):
            with torch.no_grad():
                t = torch.zeros_like(self.data)
                self.grad = t
            return self.grad
        else:
            raise NotImplemented

    def bagua_mark_communication_ready(self):
        """
        Mark a Bagua tensor ready for scheduled operations execution.
        """
        torch.cuda.current_stream().record_event(self._bagua_ready_event)
        self._bagua_backend.mark_communication_ready(
            self.backend_tensor,
            self._bagua_ready_event.cuda_event,
        )

    def bagua_set_storage(self, storage: torch.Storage, storage_offset: int = 0):
        """
        Sets the underlying storage using an existing torch.Storage.

        Args:
            storage (Storage): the storage to use
            storage_offset (int, optional): the offset in the storage
        """
        with torch.no_grad():
            self.set_(storage, storage_offset, self.shape)
        self.backend_tensor.reset_ptr(self.data_ptr())
