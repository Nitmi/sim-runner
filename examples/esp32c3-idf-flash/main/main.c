#include <stdio.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "build_identity.h"

void app_main(void)
{
    printf("SIM_IDF_C3_READY v1\n");
    printf("SIM_IDF_C3_BUILD %s\n", SIM_BUILD_ID);
    fflush(stdout);
    for (unsigned i = 1; i <= 5; ++i) {
        printf("SIM_IDF_C3_HEARTBEAT %08u\n", i);
        fflush(stdout);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
#if SIM_VARIANT == 1
    for (;;) { vTaskDelay(pdMS_TO_TICKS(100)); }
#elif SIM_VARIANT == 2
    abort();
#else
    esp_restart();
#endif
}
