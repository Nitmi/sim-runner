#include <stdio.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "build_identity.h"

static void second_core(void *argument)
{
    TaskHandle_t owner = (TaskHandle_t)argument;
    printf("SIM_IDF_S3_CORE%u v1\n", (unsigned)xPortGetCoreID());
    fflush(stdout);
    xTaskNotifyGive(owner);
    vTaskDelete(NULL);
}

void app_main(void)
{
    printf("SIM_IDF_S3_CORE%u v1\n", (unsigned)xPortGetCoreID());
    fflush(stdout);
    BaseType_t core = SIM_VARIANT == 3 ? 0 : 1;
    if (xTaskCreatePinnedToCore(second_core, "sim_core1", 2048,
                              xTaskGetCurrentTaskHandle(), 5, NULL, core) != pdPASS) {
        abort();
    }
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    printf("SIM_IDF_S3_READY v1\n");
    printf("SIM_IDF_S3_BUILD %s\n", SIM_BUILD_ID);
    fflush(stdout);
    for (unsigned i = 1; i <= 5; ++i) {
        printf("SIM_IDF_S3_HEARTBEAT %08u\n", i);
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
