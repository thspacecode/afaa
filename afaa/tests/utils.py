from afaa.tests.data.bootstrap_test_master_data import BootStrapTestMasterData
from afaa.tests.testsuite import AFAATestSuite

# Importing test utilities bootstraps shared records, following erpnext.tests.utils.
boot_strap_test_master_data = BootStrapTestMasterData()
boot_strap_test_master_data.make()
