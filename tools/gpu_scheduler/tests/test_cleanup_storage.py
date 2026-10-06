import json,socket,tempfile,unittest
from pathlib import Path
from tools.gpu_scheduler.docker_cleanup import validate_storage

class ExistingSocketStorage(unittest.TestCase):
    def test_opt_in_requires_actual_data_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);mount=base/'data';mount.mkdir();store=mount/'docker';store.mkdir()
            sock=socket.socket(socket.AF_UNIX);sock.bind(str(base/'existing.sock'))
            try:
                endpoint=base/'existing.sock'
                good=lambda *args:json.dumps(str(store))
                with self.assertRaisesRegex(ValueError,'opt-in'):
                    validate_storage(mount,endpoint,mount,mount,good)
                validate_storage(mount,endpoint,mount,mount,good,allow_existing_system_socket=True)
                with self.assertRaisesRegex(ValueError,'not on the data'):
                    validate_storage(mount,endpoint,mount,mount,lambda *args:json.dumps(str(base)),allow_existing_system_socket=True)
                with self.assertRaisesRegex(ValueError,'storage must stay'):
                    validate_storage(mount,endpoint,base,mount,good,allow_existing_system_socket=True)
            finally:sock.close()

if __name__=='__main__':unittest.main()
